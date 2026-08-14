"""工作区深度完善模块 (workspace)。

v7.150: 多租户隔离 / 成员管理 / 权限矩阵。

提供：
- 16 个 REST 端点（工作区 CRUD + 成员管理 + 邀请管理 + 权限矩阵）
- 5 角色 × 9 权限的默认矩阵（owner/admin/member/viewer/guest）
- ``check_permission(actor, permission)`` 辅助函数（同步，基于默认矩阵）
- ``check_permission_for_role(role, permission, matrix=None)`` 纯函数
- 多租户隔离：所有查询按 workspace_id 过滤，actor 必须为该工作区成员

与既有 ``workspaces.py`` / ``id_workspace`` / ``id_member`` 表独立共存，
操作新建的 ``workspace_v2`` / ``workspace_member`` / ``workspace_invite``
/ ``workspace_role_permission`` 表。

设计文档：910-进度追踪与任务清单.md「P1 工作区深度完善」
"""
from __future__ import annotations

import re
import secrets
from datetime import UTC, datetime, timedelta
from typing import Annotated, Any, Literal

from fastapi import APIRouter, Depends, HTTPException, Query, Response, status
from pydantic import BaseModel, EmailStr, Field

from workama_platform.core import (
    Actor,
    get_actor,
    json_dumps,
    new_id,
    pool,
)

# ============================================================================
# 常量
# ============================================================================

router = APIRouter(prefix="/api/v1/workspaces", tags=["workspace"])

# 5 个角色
ROLES: tuple[str, ...] = ("owner", "admin", "member", "viewer", "guest")
MANAGEMENT_ROLES: frozenset[str] = frozenset({"owner", "admin"})
ASSIGNABLE_ROLES: frozenset[str] = frozenset({"admin", "member", "viewer", "guest"})
INVITE_ROLES: frozenset[str] = frozenset({"admin", "member", "viewer", "guest"})

# 9 个权限
PERMISSIONS: tuple[str, ...] = (
    "workspace.read",
    "workspace.write",
    "workspace.delete",
    "member.invite",
    "member.remove",
    "billing.read",
    "billing.write",
    "settings.read",
    "settings.write",
)

# 默认权限矩阵：DEFAULT_PERMISSION_MATRIX[role][permission] -> bool
DEFAULT_PERMISSION_MATRIX: dict[str, dict[str, bool]] = {
    "owner": {p: True for p in PERMISSIONS},
    "admin": {
        "workspace.read": True,
        "workspace.write": True,
        "workspace.delete": False,
        "member.invite": True,
        "member.remove": True,
        "billing.read": True,
        "billing.write": True,
        "settings.read": True,
        "settings.write": True,
    },
    "member": {
        "workspace.read": True,
        "workspace.write": True,
        "workspace.delete": False,
        "member.invite": False,
        "member.remove": False,
        "billing.read": False,
        "billing.write": False,
        "settings.read": True,
        "settings.write": False,
    },
    "viewer": {
        "workspace.read": True,
        "workspace.write": False,
        "workspace.delete": False,
        "member.invite": False,
        "member.remove": False,
        "billing.read": False,
        "billing.write": False,
        "settings.read": True,
        "settings.write": False,
    },
    "guest": {
        "workspace.read": True,
        "workspace.write": False,
        "workspace.delete": False,
        "member.invite": False,
        "member.remove": False,
        "billing.read": False,
        "billing.write": False,
        "settings.read": False,
        "settings.write": False,
    },
}

DEFAULT_INVITE_TTL_SECONDS = 7 * 24 * 3600
MAX_INVITE_TTL_SECONDS = 30 * 24 * 3600
_SLUG_RE = re.compile(r"^[a-z0-9](?:[a-z0-9-]{0,62}[a-z0-9])?$")

WorkspaceRole = Literal["owner", "admin", "member", "viewer", "guest"]
MemberStatus = Literal["active", "invited", "suspended"]
InviteStatus = Literal["pending", "accepted", "revoked", "expired"]


# ============================================================================
# Pydantic 模型
# ============================================================================


class WorkspaceCreateRequest(BaseModel):
    name: str = Field(min_length=1, max_length=80)
    slug: str = Field(min_length=1, max_length=64)
    description: str | None = Field(default=None, max_length=2000)
    plan: str = Field(default="free", max_length=32)
    settings: dict[str, Any] = Field(default_factory=dict)


class WorkspaceUpdateRequest(BaseModel):
    name: str | None = Field(default=None, min_length=1, max_length=80)
    description: str | None = Field(default=None, max_length=2000)
    settings: dict[str, Any] | None = None


class MemberAddRequest(BaseModel):
    user_id: str = Field(min_length=1, max_length=128)
    role: WorkspaceRole = "member"


class MemberUpdateRequest(BaseModel):
    role: WorkspaceRole


class InviteCreateRequest(BaseModel):
    email: EmailStr
    role: WorkspaceRole = "member"
    expires_in_seconds: int = Field(
        default=DEFAULT_INVITE_TTL_SECONDS, ge=60, le=MAX_INVITE_TTL_SECONDS
    )


class PermissionMatrixUpdateRequest(BaseModel):
    """整矩阵替换：``{role: {permission: bool}}``。"""
    matrix: dict[str, dict[str, bool]]


# ============================================================================
# 辅助函数
# ============================================================================


def normalize_slug(value: str) -> str:
    slug = value.strip().lower()
    if not _SLUG_RE.fullmatch(slug):
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="Slug must contain only lowercase letters, numbers, and internal hyphens",
        )
    return slug


def normalize_email(value: str) -> str:
    return value.strip().lower()


def check_permission_for_role(
    role: str, permission: str, matrix: dict[str, dict[str, bool]] | None = None
) -> bool:
    """纯函数：检查角色在给定矩阵（或默认矩阵）下是否拥有权限。"""
    src = matrix if matrix is not None else DEFAULT_PERMISSION_MATRIX
    return bool(src.get(role, {}).get(permission, False))


def check_permission(actor: Actor, permission: str) -> bool:
    """同步辅助函数：基于 actor.role 与默认权限矩阵检查。

    注意：此函数仅检查默认矩阵，不考虑工作区级别的自定义覆盖。
    需要考虑自定义矩阵时使用 ``_check_workspace_permission``。
    """
    return check_permission_for_role(actor.role, permission)


async def _get_member_row(
    conn: Any, actor: Actor, ws_id: str
) -> dict[str, Any] | None:
    """查询 actor 在指定工作区的成员记录，不存在返回 None。"""
    result = await conn.execute(
        "SELECT * FROM workspace_member WHERE workspace_id = %s AND user_id = %s",
        (ws_id, actor.user_id),
    )
    return await result.fetchone()


async def _require_member(
    conn: Any, actor: Actor, ws_id: str
) -> dict[str, Any]:
    """要求 actor 是指定工作区的活跃成员，否则 403/404。

    - 工作区不存在 → 404
    - 工作区存在但 actor 非成员 → 403
    - 成员但被 suspended → 403
    """
    ws_result = await conn.execute(
        "SELECT id, org_id, status FROM workspace_v2 WHERE id = %s",
        (ws_id,),
    )
    ws_row = await ws_result.fetchone()
    if not ws_row:
        raise HTTPException(status_code=404, detail="Workspace not found")
    member = await _get_member_row(conn, actor, ws_id)
    if not member:
        raise HTTPException(
            status_code=403, detail="Actor is not a member of this workspace"
        )
    if member["status"] != "active":
        raise HTTPException(
            status_code=403, detail="Member is not active in this workspace"
        )
    return member


async def _load_permission_matrix(
    conn: Any, ws_id: str
) -> dict[str, dict[str, bool]]:
    """加载工作区的权限矩阵（DB 行覆盖默认矩阵）。"""
    result = await conn.execute(
        "SELECT role, permission, granted FROM workspace_role_permission WHERE workspace_id = %s",
        (ws_id,),
    )
    rows = await result.fetchall()
    # 以默认矩阵为底，DB 行覆盖
    matrix: dict[str, dict[str, bool]] = {
        role: dict(DEFAULT_PERMISSION_MATRIX[role]) for role in ROLES
    }
    for row in rows:
        role = row["role"]
        perm = row["permission"]
        if role in matrix and perm in matrix[role]:
            matrix[role][perm] = bool(row["granted"])
    return matrix


async def _check_workspace_permission(
    conn: Any, actor: Actor, ws_id: str, permission: str
) -> bool:
    """检查 actor 在指定工作区是否拥有 permission（考虑自定义矩阵）。"""
    member = await _get_member_row(conn, actor, ws_id)
    if not member or member["status"] != "active":
        return False
    matrix = await _load_permission_matrix(conn, ws_id)
    return check_permission_for_role(member["role"], permission, matrix)


async def _require_workspace_permission(
    conn: Any, actor: Actor, ws_id: str, permission: str
) -> dict[str, Any]:
    """要求 actor 在工作区拥有 permission，否则 403。先校验成员身份。

    复用 ``_require_member`` 已查到的成员行，避免重复查询。
    """
    member = await _require_member(conn, actor, ws_id)
    matrix = await _load_permission_matrix(conn, ws_id)
    if not check_permission_for_role(member["role"], permission, matrix):
        raise HTTPException(
            status_code=403, detail=f"Missing workspace permission: {permission}"
        )
    return member


async def _seed_default_permissions(conn: Any, ws_id: str) -> None:
    """为工作区写入默认权限矩阵行。"""
    for role in ROLES:
        for perm in PERMISSIONS:
            granted = DEFAULT_PERMISSION_MATRIX[role][perm]
            await conn.execute(
                """
                INSERT INTO workspace_role_permission(id, workspace_id, role, permission, granted)
                VALUES (%s, %s, %s, %s, %s)
                ON CONFLICT (workspace_id, role, permission) DO UPDATE SET granted = EXCLUDED.granted
                """,
                (new_id("wsrp"), ws_id, role, perm, granted),
            )


def _workspace_response(row: dict[str, Any]) -> dict[str, Any]:
    return {
        "id": row["id"],
        "org_id": row["org_id"],
        "name": row["name"],
        "slug": row["slug"],
        "description": row.get("description"),
        "plan": row.get("plan", "free"),
        "status": row.get("status", "active"),
        "settings": row.get("settings") or {},
        "created_at": row.get("created_at"),
        "updated_at": row.get("updated_at"),
    }


def _member_response(row: dict[str, Any]) -> dict[str, Any]:
    return {
        "id": row["id"],
        "workspace_id": row["workspace_id"],
        "user_id": row["user_id"],
        "role": row["role"],
        "status": row["status"],
        "joined_at": row.get("joined_at"),
        "metadata": row.get("metadata") or {},
    }


def _invite_response(row: dict[str, Any]) -> dict[str, Any]:
    return {
        "id": row["id"],
        "workspace_id": row["workspace_id"],
        "email": row["email"],
        "role": row["role"],
        "invited_by": row["invited_by"],
        "status": row["status"],
        "expires_at": row.get("expires_at"),
        "accepted_at": row.get("accepted_at"),
        "created_at": row.get("created_at"),
    }


# ============================================================================
# Router 端点
# ============================================================================
#
# 路由声明顺序：``/invites/...`` 必须在 ``/{ws_id}`` 之前声明，
# 否则 FastAPI 会将 "invites" 当作 ws_id 匹配。


@router.post("", status_code=status.HTTP_201_CREATED)
async def create_workspace(
    body: WorkspaceCreateRequest,
    actor: Annotated[Actor, Depends(get_actor)],
):
    """创建工作区：actor 自动成为 owner，并写入默认权限矩阵。"""
    slug = normalize_slug(body.slug)
    async with pool.connection() as conn:
        async with conn.transaction():
            # slug 在 org 内唯一
            existing = await conn.execute(
                "SELECT id FROM workspace_v2 WHERE org_id = %s AND slug = %s",
                (actor.org_id, slug),
            )
            if await existing.fetchone():
                raise HTTPException(
                    status_code=409, detail="A workspace with this slug already exists"
                )
            ws_id = new_id("wsp")
            await conn.execute(
                """
                INSERT INTO workspace_v2(
                    id, org_id, name, slug, description, plan, status, settings
                ) VALUES (%s, %s, %s, %s, %s, %s, 'active', %s::jsonb)
                """,
                (
                    ws_id,
                    actor.org_id,
                    body.name.strip(),
                    slug,
                    body.description,
                    body.plan,
                    json_dumps(body.settings),
                ),
            )
            # actor 成为 owner
            await conn.execute(
                """
                INSERT INTO workspace_member(id, workspace_id, user_id, role, status, metadata)
                VALUES (%s, %s, %s, 'owner', 'active', '{}'::jsonb)
                """,
                (new_id("mem"), ws_id, actor.user_id),
            )
            # 写入默认权限矩阵
            await _seed_default_permissions(conn, ws_id)
            row_result = await conn.execute(
                "SELECT * FROM workspace_v2 WHERE id = %s",
                (ws_id,),
            )
            row = await row_result.fetchone()
    return _workspace_response(row)


@router.get("")
async def list_workspaces(
    actor: Annotated[Actor, Depends(get_actor)],
    limit: int = Query(default=20, ge=1, le=200),
    offset: int = Query(default=0, ge=0),
):
    """列出当前用户所在的工作区（按 workspace_member 关系）。"""
    async with pool.connection() as conn:
        result = await conn.execute(
            """
            SELECT w.* FROM workspace_v2 w
            JOIN workspace_member m ON m.workspace_id = w.id
            WHERE m.user_id = %s AND m.status = 'active' AND w.status = 'active'
            ORDER BY w.created_at DESC, w.id DESC
            LIMIT %s OFFSET %s
            """,
            (actor.user_id, limit, offset),
        )
        rows = await result.fetchall()
    items = [_workspace_response(row) for row in rows]
    return {
        "items": items,
        "data": items,
        "count": len(items),
        "limit": limit,
        "offset": offset,
    }


@router.get("/invites/{invite_id}")
async def get_invite(
    invite_id: str,
    actor: Annotated[Actor, Depends(get_actor)],
):
    """查询邀请详情（仅限邀请关联工作区的成员可见）。"""
    async with pool.connection() as conn:
        inv_result = await conn.execute(
            "SELECT * FROM workspace_invite WHERE id = %s",
            (invite_id,),
        )
        invite = await inv_result.fetchone()
        if not invite:
            raise HTTPException(status_code=404, detail="Invite not found")
        await _require_member(conn, actor, invite["workspace_id"])
    return _invite_response(invite)


@router.delete("/invites/{invite_id}", status_code=status.HTTP_204_NO_CONTENT)
async def revoke_invite(
    invite_id: str,
    actor: Annotated[Actor, Depends(get_actor)],
):
    """撤销邀请（需 member.invite 权限）。"""
    async with pool.connection() as conn:
        async with conn.transaction():
            inv_result = await conn.execute(
                "SELECT * FROM workspace_invite WHERE id = %s FOR UPDATE",
                (invite_id,),
            )
            invite = await inv_result.fetchone()
            if not invite:
                raise HTTPException(status_code=404, detail="Invite not found")
            await _require_workspace_permission(
                conn, actor, invite["workspace_id"], "member.invite"
            )
            if invite["status"] == "accepted":
                raise HTTPException(
                    status_code=409, detail="Accepted invite cannot be revoked"
                )
            await conn.execute(
                "UPDATE workspace_invite SET status = 'revoked' WHERE id = %s",
                (invite_id,),
            )
    return Response(status_code=204)


@router.post("/invites/{token}/accept")
async def accept_invite(
    token: str,
    actor: Annotated[Actor, Depends(get_actor)],
):
    """接受邀请：校验 token / 状态 / 过期，加入 workspace_member。"""
    async with pool.connection() as conn:
        async with conn.transaction():
            inv_result = await conn.execute(
                "SELECT * FROM workspace_invite WHERE token = %s FOR UPDATE",
                (token,),
            )
            invite = await inv_result.fetchone()
            if not invite:
                raise HTTPException(status_code=404, detail="Invite not found")
            if invite["status"] == "accepted":
                raise HTTPException(
                    status_code=409, detail="Invite has already been accepted"
                )
            if invite["status"] == "revoked":
                raise HTTPException(
                    status_code=410, detail="Invite has been revoked"
                )
            expires_at = invite["expires_at"]
            if expires_at.tzinfo is None:
                expires_at = expires_at.replace(tzinfo=UTC)
            if expires_at <= datetime.now(UTC):
                await conn.execute(
                    "UPDATE workspace_invite SET status = 'expired' WHERE id = %s",
                    (invite["id"],),
                )
                raise HTTPException(status_code=410, detail="Invite has expired")
            # 已是成员则更新角色
            existing = await conn.execute(
                "SELECT id FROM workspace_member WHERE workspace_id = %s AND user_id = %s",
                (invite["workspace_id"], actor.user_id),
            )
            existing_member = await existing.fetchone()
            if existing_member:
                await conn.execute(
                    "UPDATE workspace_member SET role = %s, status = 'active' WHERE id = %s",
                    (invite["role"], existing_member["id"]),
                )
                member_id = existing_member["id"]
            else:
                member_id = new_id("mem")
                await conn.execute(
                    """
                    INSERT INTO workspace_member(id, workspace_id, user_id, role, status, metadata)
                    VALUES (%s, %s, %s, %s, 'active', '{}'::jsonb)
                    """,
                    (member_id, invite["workspace_id"], actor.user_id, invite["role"]),
                )
            await conn.execute(
                "UPDATE workspace_invite SET status = 'accepted', accepted_at = now() WHERE id = %s",
                (invite["id"],),
            )
            ws_result = await conn.execute(
                "SELECT * FROM workspace_v2 WHERE id = %s",
                (invite["workspace_id"],),
            )
            ws_row = await ws_result.fetchone()
    return {
        "accepted": True,
        "invite_id": invite["id"],
        "membership_id": member_id,
        "workspace_id": invite["workspace_id"],
        "workspace": _workspace_response(ws_row) if ws_row else None,
        "role": invite["role"],
    }


@router.get("/{ws_id}")
async def get_workspace(
    ws_id: str,
    actor: Annotated[Actor, Depends(get_actor)],
):
    """工作区详情：必须是成员。"""
    async with pool.connection() as conn:
        await _require_member(conn, actor, ws_id)
        result = await conn.execute(
            "SELECT * FROM workspace_v2 WHERE id = %s",
            (ws_id,),
        )
        row = await result.fetchone()
    if not row:
        raise HTTPException(status_code=404, detail="Workspace not found")
    return _workspace_response(row)


@router.patch("/{ws_id}")
async def update_workspace(
    ws_id: str,
    body: WorkspaceUpdateRequest,
    actor: Annotated[Actor, Depends(get_actor)],
):
    """更新工作区 name/description/settings：需 workspace.write 权限。"""
    async with pool.connection() as conn:
        async with conn.transaction():
            await _require_workspace_permission(conn, actor, ws_id, "workspace.write")
            sets: list[str] = []
            params: list[object] = []
            if body.name is not None:
                sets.append("name = %s")
                params.append(body.name.strip())
            if body.description is not None:
                sets.append("description = %s")
                params.append(body.description)
            if body.settings is not None:
                sets.append("settings = %s::jsonb")
                params.append(json_dumps(body.settings))
            if not sets:
                sets.append("updated_at = now()")
            sets.append("updated_at = now()")
            params.append(ws_id)
            result = await conn.execute(
                f"UPDATE workspace_v2 SET {', '.join(sets)} WHERE id = %s RETURNING *",
                tuple(params),
            )
            row = await result.fetchone()
    if not row:
        raise HTTPException(status_code=404, detail="Workspace not found")
    return _workspace_response(row)


@router.delete("/{ws_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_workspace(
    ws_id: str,
    actor: Annotated[Actor, Depends(get_actor)],
):
    """删除工作区：仅 owner（需 workspace.delete 权限）。"""
    async with pool.connection() as conn:
        async with conn.transaction():
            member = await _require_workspace_permission(
                conn, actor, ws_id, "workspace.delete"
            )
            if member["role"] != "owner":
                raise HTTPException(
                    status_code=403, detail="Only owner can delete workspace"
                )
            await conn.execute(
                "DELETE FROM workspace_role_permission WHERE workspace_id = %s",
                (ws_id,),
            )
            await conn.execute(
                "DELETE FROM workspace_member WHERE workspace_id = %s",
                (ws_id,),
            )
            await conn.execute(
                "DELETE FROM workspace_invite WHERE workspace_id = %s",
                (ws_id,),
            )
            await conn.execute(
                "DELETE FROM workspace_v2 WHERE id = %s",
                (ws_id,),
            )
    return Response(status_code=204)


@router.get("/{ws_id}/members")
async def list_members(
    ws_id: str,
    actor: Annotated[Actor, Depends(get_actor)],
    role_filter: str | None = Query(default=None),
    limit: int = Query(default=50, ge=1, le=500),
    offset: int = Query(default=0, ge=0),
):
    """成员列表：必须是成员。"""
    async with pool.connection() as conn:
        await _require_member(conn, actor, ws_id)
        clause = ""
        params: list[object] = [ws_id]
        if role_filter:
            clause = "AND role = %s"
            params.append(role_filter)
        params.append(limit)
        params.append(offset)
        result = await conn.execute(
            f"""
            SELECT * FROM workspace_member
            WHERE workspace_id = %s {clause}
            ORDER BY joined_at DESC, id DESC
            LIMIT %s OFFSET %s
            """,
            tuple(params),
        )
        rows = await result.fetchall()
    items = [_member_response(row) for row in rows]
    return {
        "items": items,
        "data": items,
        "count": len(items),
        "limit": limit,
        "offset": offset,
    }


@router.post("/{ws_id}/members", status_code=status.HTTP_201_CREATED)
async def add_member(
    ws_id: str,
    body: MemberAddRequest,
    actor: Annotated[Actor, Depends(get_actor)],
):
    """添加成员（直接添加，需 member.invite 权限）。"""
    if body.role == "owner":
        raise HTTPException(
            status_code=400, detail="Cannot assign owner role via direct add"
        )
    async with pool.connection() as conn:
        async with conn.transaction():
            await _require_workspace_permission(conn, actor, ws_id, "member.invite")
            existing = await conn.execute(
                "SELECT id FROM workspace_member WHERE workspace_id = %s AND user_id = %s",
                (ws_id, body.user_id),
            )
            if await existing.fetchone():
                raise HTTPException(
                    status_code=409, detail="User is already a member"
                )
            member_id = new_id("mem")
            await conn.execute(
                """
                INSERT INTO workspace_member(id, workspace_id, user_id, role, status, metadata)
                VALUES (%s, %s, %s, %s, 'active', '{}'::jsonb)
                """,
                (member_id, ws_id, body.user_id, body.role),
            )
            result = await conn.execute(
                "SELECT * FROM workspace_member WHERE id = %s",
                (member_id,),
            )
            row = await result.fetchone()
    return _member_response(row)


@router.patch("/{ws_id}/members/{user_id}")
async def update_member_role(
    ws_id: str,
    user_id: str,
    body: MemberUpdateRequest,
    actor: Annotated[Actor, Depends(get_actor)],
):
    """更新成员角色（需 member.invite 权限，不能将成员提升为 owner，不能降级最后 owner）。"""
    if body.role == "owner":
        raise HTTPException(
            status_code=400, detail="Cannot promote to owner via role update"
        )
    async with pool.connection() as conn:
        async with conn.transaction():
            await _require_workspace_permission(conn, actor, ws_id, "member.invite")
            target_result = await conn.execute(
                "SELECT * FROM workspace_member WHERE workspace_id = %s AND user_id = %s FOR UPDATE",
                (ws_id, user_id),
            )
            target = await target_result.fetchone()
            if not target:
                raise HTTPException(status_code=404, detail="Member not found")
            if target["role"] == "owner":
                raise HTTPException(
                    status_code=400, detail="Cannot change owner role"
                )
            await conn.execute(
                "UPDATE workspace_member SET role = %s WHERE id = %s",
                (body.role, target["id"]),
            )
            result = await conn.execute(
                "SELECT * FROM workspace_member WHERE id = %s",
                (target["id"],),
            )
            row = await result.fetchone()
    return _member_response(row)


@router.delete("/{ws_id}/members/{user_id}", status_code=status.HTTP_204_NO_CONTENT)
async def remove_member(
    ws_id: str,
    user_id: str,
    actor: Annotated[Actor, Depends(get_actor)],
):
    """移除成员（需 member.remove 权限，不能移除最后一个 owner）。"""
    async with pool.connection() as conn:
        async with conn.transaction():
            await _require_workspace_permission(conn, actor, ws_id, "member.remove")
            target_result = await conn.execute(
                "SELECT * FROM workspace_member WHERE workspace_id = %s AND user_id = %s FOR UPDATE",
                (ws_id, user_id),
            )
            target = await target_result.fetchone()
            if not target:
                raise HTTPException(status_code=404, detail="Member not found")
            if target["role"] == "owner":
                # 不能移除 owner；若要离开工作区需走 transfer/delete 流程
                raise HTTPException(
                    status_code=400, detail="Cannot remove owner; transfer ownership first"
                )
            await conn.execute(
                "DELETE FROM workspace_member WHERE id = %s",
                (target["id"],),
            )
    return Response(status_code=204)


@router.post("/{ws_id}/invites", status_code=status.HTTP_201_CREATED)
async def create_invite(
    ws_id: str,
    body: InviteCreateRequest,
    actor: Annotated[Actor, Depends(get_actor)],
):
    """创建邀请（需 member.invite 权限）。"""
    if body.role == "owner":
        raise HTTPException(
            status_code=400, detail="Cannot invite as owner"
        )
    async with pool.connection() as conn:
        async with conn.transaction():
            await _require_workspace_permission(conn, actor, ws_id, "member.invite")
            email = normalize_email(str(body.email))
            # 同 email pending 邀请已存在
            existing = await conn.execute(
                "SELECT id FROM workspace_invite WHERE workspace_id = %s AND email = %s AND status = 'pending'",
                (ws_id, email),
            )
            if await existing.fetchone():
                raise HTTPException(
                    status_code=409, detail="An active invite already exists for this email"
                )
            invite_id = new_id("inv")
            token = secrets.token_urlsafe(32)
            expires_at = datetime.now(UTC) + timedelta(seconds=body.expires_in_seconds)
            await conn.execute(
                """
                INSERT INTO workspace_invite(
                    id, workspace_id, email, role, token, invited_by, status, expires_at
                ) VALUES (%s, %s, %s, %s, %s, %s, 'pending', %s)
                """,
                (invite_id, ws_id, email, body.role, token, actor.user_id, expires_at),
            )
            result = await conn.execute(
                "SELECT * FROM workspace_invite WHERE id = %s",
                (invite_id,),
            )
            row = await result.fetchone()
    response = _invite_response(row)
    response["token"] = token
    return response


@router.get("/{ws_id}/invites")
async def list_invites(
    ws_id: str,
    actor: Annotated[Actor, Depends(get_actor)],
    status_filter: InviteStatus | None = Query(default=None, alias="status"),
    limit: int = Query(default=50, ge=1, le=500),
    offset: int = Query(default=0, ge=0),
):
    """邀请列表（需 member.invite 权限，admin/owner 默认满足）。"""
    async with pool.connection() as conn:
        await _require_workspace_permission(conn, actor, ws_id, "member.invite")
        # 过期未接受的邀请先标记 expired
        await conn.execute(
            "UPDATE workspace_invite SET status = 'expired' "
            "WHERE workspace_id = %s AND status = 'pending' AND expires_at <= now()",
            (ws_id,),
        )
        clause = ""
        params: list[object] = [ws_id]
        if status_filter:
            clause = "AND status = %s"
            params.append(status_filter)
        params.append(limit)
        params.append(offset)
        result = await conn.execute(
            f"""
            SELECT * FROM workspace_invite
            WHERE workspace_id = %s {clause}
            ORDER BY created_at DESC, id DESC
            LIMIT %s OFFSET %s
            """,
            tuple(params),
        )
        rows = await result.fetchall()
    items = [_invite_response(row) for row in rows]
    return {
        "items": items,
        "data": items,
        "count": len(items),
        "limit": limit,
        "offset": offset,
    }


@router.get("/{ws_id}/permissions")
async def get_permissions(
    ws_id: str,
    actor: Annotated[Actor, Depends(get_actor)],
):
    """权限矩阵：必须是成员。"""
    async with pool.connection() as conn:
        await _require_member(conn, actor, ws_id)
        matrix = await _load_permission_matrix(conn, ws_id)
    return {
        "workspace_id": ws_id,
        "matrix": matrix,
        "roles": list(ROLES),
        "permissions": list(PERMISSIONS),
    }


@router.put("/{ws_id}/permissions")
async def update_permissions(
    ws_id: str,
    body: PermissionMatrixUpdateRequest,
    actor: Annotated[Actor, Depends(get_actor)],
):
    """更新权限矩阵：仅 owner。整矩阵替换。"""
    async with pool.connection() as conn:
        async with conn.transaction():
            member = await _require_member(conn, actor, ws_id)
            if member["role"] != "owner":
                raise HTTPException(
                    status_code=403, detail="Only owner can update permission matrix"
                )
            # 校验输入合法性
            for role, perms in body.matrix.items():
                if role not in ROLES:
                    raise HTTPException(
                        status_code=422, detail=f"Unknown role: {role}"
                    )
                for perm, granted in perms.items():
                    if perm not in PERMISSIONS:
                        raise HTTPException(
                            status_code=422, detail=f"Unknown permission: {perm}"
                        )
                    if not isinstance(granted, bool):
                        raise HTTPException(
                            status_code=422,
                            detail=f"granted must be bool for {role}.{perm}",
                        )
            # 整矩阵替换：先删后插
            await conn.execute(
                "DELETE FROM workspace_role_permission WHERE workspace_id = %s",
                (ws_id,),
            )
            for role in ROLES:
                for perm in PERMISSIONS:
                    granted = body.matrix.get(role, {}).get(
                        perm, DEFAULT_PERMISSION_MATRIX[role][perm]
                    )
                    await conn.execute(
                        """
                        INSERT INTO workspace_role_permission(id, workspace_id, role, permission, granted)
                        VALUES (%s, %s, %s, %s, %s)
                        """,
                        (new_id("wsrp"), ws_id, role, perm, granted),
                    )
            matrix = await _load_permission_matrix(conn, ws_id)
    return {
        "workspace_id": ws_id,
        "matrix": matrix,
        "roles": list(ROLES),
        "permissions": list(PERMISSIONS),
    }
