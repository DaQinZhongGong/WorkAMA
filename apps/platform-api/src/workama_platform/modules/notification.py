"""平台支撑模块 - 通知中心 (notification)。

v7.153: P1 平台支撑模块（通知/文件/搜索）。

提供：
- 7 个 REST 端点（创建 / 列表 / 详情 / 标记已读 / 全部已读 / 删除 / 未读数量）
- ``create_notification`` 辅助函数供其他模块调用

注意：本模块与同目录 ``notification/`` 包同名。``notification/`` 是常规包
（有 ``__init__.py``），遮蔽本文件，因此 ``main.py`` 通过 importlib 按文件
路径加载本模块（与 ``billing.py`` 的处理方式完全一致）。

为避免与既有 ``notification/`` 包（``id_notification`` 表、preferences、
deliveries 等）的路由冲突，本模块使用独立前缀 ``/api/v1/notification-center``，
对应新建的 ``notification`` 表，提供更简洁的 in-app 通知 CRUD/未读数接口。

设计文档：910-进度追踪与任务清单.md「P1 平台支撑模块（通知/文件/搜索）」
"""
from __future__ import annotations

from datetime import datetime
from typing import Annotated, Any, Literal

from fastapi import APIRouter, Depends, HTTPException, Query, status
from pydantic import BaseModel, Field

from workama_platform.core import (
    Actor,
    capability_allows,
    get_actor,
    json_dumps,
    new_id,
    pool,
)

router = APIRouter(
    prefix="/api/v1/notification-center", tags=["notification-center"]
)

NotificationKind = Literal["info", "success", "warning", "error", "system"]
_VALID_KINDS: frozenset[str] = frozenset(
    {"info", "success", "warning", "error", "system"}
)


class NotificationCreateRequest(BaseModel):
    user_id: str = Field(min_length=1, max_length=200)
    kind: NotificationKind = "info"
    title: str = Field(min_length=1, max_length=200)
    body: str | None = Field(default=None, max_length=4000)
    action_url: str | None = Field(default=None, max_length=500)
    action_label: str | None = Field(default=None, max_length=100)
    metadata: dict[str, Any] = Field(default_factory=dict)


class Notification(BaseModel):
    id: str
    workspace_id: str
    user_id: str
    kind: str
    title: str
    body: str | None = None
    action_url: str | None = None
    action_label: str | None = None
    read: bool
    metadata: dict[str, Any] = Field(default_factory=dict)
    created_at: datetime
    read_at: datetime | None = None


# NotificationResponse 作为 Notification 的别名导出，保持 API 命名一致
NotificationResponse = Notification


def _require(actor: Actor, action: str) -> None:
    if not capability_allows(actor.capabilities, f"notification:{action}"):
        raise HTTPException(
            status_code=403, detail=f"Missing capability: notification:{action}"
        )


def _require_admin(actor: Actor) -> None:
    if actor.role not in {"owner", "admin"}:
        raise HTTPException(
            status_code=403, detail="Admin role required to create notifications"
        )


def _summary(row: dict) -> dict:
    return {
        "id": row["id"],
        "workspace_id": row["workspace_id"],
        "user_id": row["user_id"],
        "kind": row["kind"],
        "title": row["title"],
        "body": row.get("body"),
        "action_url": row.get("action_url"),
        "action_label": row.get("action_label"),
        "read": row["read"],
        "metadata": row.get("metadata") or {},
        "created_at": row["created_at"],
        "read_at": row.get("read_at"),
    }


async def create_notification(
    workspace_id: str,
    user_id: str,
    kind: str = "info",
    title: str = "",
    body: str | None = None,
    action_url: str | None = None,
    action_label: str | None = None,
    metadata: dict[str, Any] | None = None,
) -> dict:
    """创建通知（供其他模块调用）。``kind`` 非法时回退为 ``info``。"""
    if kind not in _VALID_KINDS:
        kind = "info"
    notif_id = new_id("notif")
    async with pool.connection() as conn:
        async with conn.transaction():
            result = await conn.execute(
                """
                INSERT INTO notification(
                    id, workspace_id, user_id, kind, title, body,
                    action_url, action_label, read, metadata)
                VALUES (%s, %s, %s, %s, %s, %s, %s, %s, FALSE, %s::jsonb)
                RETURNING *
                """,
                (
                    notif_id,
                    workspace_id,
                    user_id,
                    kind,
                    title,
                    body,
                    action_url,
                    action_label,
                    json_dumps(metadata or {}),
                ),
            )
            row = await result.fetchone()
    return _summary(row)


# ============================================================================
# Router 端点
# ============================================================================
#
# 路由声明顺序：具体路径（/unread-count, /read-all）必须在参数化路径 /{id}
# 之前声明，否则 FastAPI 会将 "unread-count" 当作 id 参数匹配。


@router.post("", status_code=status.HTTP_201_CREATED)
async def create_notification_endpoint(
    body: NotificationCreateRequest,
    actor: Annotated[Actor, Depends(get_actor)],
):
    """创建通知（admin/system）。可向本 workspace 内任意用户发送。"""
    _require_admin(actor)
    return await create_notification(
        workspace_id=actor.workspace_id,
        user_id=body.user_id,
        kind=body.kind,
        title=body.title,
        body=body.body,
        action_url=body.action_url,
        action_label=body.action_label,
        metadata=body.metadata,
    )


@router.get("/unread-count")
async def unread_count(
    actor: Annotated[Actor, Depends(get_actor)],
):
    """当前用户未读通知数量。"""
    _require(actor, "read")
    async with pool.connection() as conn:
        result = await conn.execute(
            """
            SELECT count(*) AS count
            FROM notification
            WHERE user_id = %s AND workspace_id = %s AND read = FALSE
            """,
            (actor.user_id, actor.workspace_id),
        )
        row = await result.fetchone()
    count = int(row["count"]) if row else 0
    return {"unread_count": count}


@router.post("/read-all")
async def mark_all_read(
    actor: Annotated[Actor, Depends(get_actor)],
):
    """将当前用户所有未读通知标记为已读。"""
    _require(actor, "write")
    async with pool.connection() as conn:
        async with conn.transaction():
            result = await conn.execute(
                """
                UPDATE notification
                SET read = TRUE, read_at = now()
                WHERE user_id = %s AND workspace_id = %s AND read = FALSE
                RETURNING id
                """,
                (actor.user_id, actor.workspace_id),
            )
            rows = await result.fetchall()
    return {"updated": len(rows)}


@router.get("")
async def list_notifications(
    actor: Annotated[Actor, Depends(get_actor)],
    unread_only: bool = Query(default=False),
    kind: NotificationKind | None = Query(default=None),
    limit: int = Query(default=50, ge=1, le=200),
    offset: int = Query(default=0, ge=0),
):
    """列表：当前用户通知，支持 unread_only / kind 过滤。"""
    _require(actor, "read")
    async with pool.connection() as conn:
        clauses = ["user_id = %s", "workspace_id = %s"]
        params: list[object] = [actor.user_id, actor.workspace_id]
        if unread_only:
            clauses.append("read = FALSE")
        if kind:
            clauses.append("kind = %s")
            params.append(kind)
        where = " AND ".join(clauses)
        list_params = list(params) + [limit, offset]
        result = await conn.execute(
            f"""
            SELECT * FROM notification
            WHERE {where}
            ORDER BY created_at DESC, id DESC LIMIT %s OFFSET %s
            """,
            tuple(list_params),
        )
        rows = await result.fetchall()
        count_result = await conn.execute(
            f"SELECT count(*) AS count FROM notification WHERE {where}",
            tuple(params),
        )
        count_row = await count_result.fetchone()
        unread_result = await conn.execute(
            """
            SELECT count(*) AS count FROM notification
            WHERE user_id = %s AND workspace_id = %s AND read = FALSE
            """,
            (actor.user_id, actor.workspace_id),
        )
        unread_row = await unread_result.fetchone()
    items = [_summary(row) for row in rows]
    return {
        "items": items,
        "data": items,
        "count": len(items),
        "total": int(count_row["count"]) if count_row else 0,
        "unread_count": int(unread_row["count"]) if unread_row else 0,
        "limit": limit,
        "offset": offset,
    }


@router.get("/{notification_id}")
async def get_notification(
    notification_id: str,
    actor: Annotated[Actor, Depends(get_actor)],
):
    """通知详情。"""
    _require(actor, "read")
    async with pool.connection() as conn:
        result = await conn.execute(
            """
            SELECT * FROM notification
            WHERE id = %s AND user_id = %s AND workspace_id = %s
            """,
            (notification_id, actor.user_id, actor.workspace_id),
        )
        row = await result.fetchone()
    if not row:
        raise HTTPException(status_code=404, detail="Notification not found")
    return _summary(row)


@router.post("/{notification_id}/read")
async def mark_read(
    notification_id: str,
    actor: Annotated[Actor, Depends(get_actor)],
):
    """标记单条通知为已读。"""
    _require(actor, "write")
    async with pool.connection() as conn:
        async with conn.transaction():
            result = await conn.execute(
                """
                UPDATE notification
                SET read = TRUE, read_at = COALESCE(read_at, now())
                WHERE id = %s AND user_id = %s AND workspace_id = %s
                RETURNING *
                """,
                (notification_id, actor.user_id, actor.workspace_id),
            )
            row = await result.fetchone()
    if not row:
        raise HTTPException(status_code=404, detail="Notification not found")
    return _summary(row)


@router.delete("/{notification_id}", status_code=status.HTTP_200_OK)
async def delete_notification(
    notification_id: str,
    actor: Annotated[Actor, Depends(get_actor)],
):
    """删除通知（硬删除）。"""
    _require(actor, "delete")
    async with pool.connection() as conn:
        async with conn.transaction():
            result = await conn.execute(
                """
                DELETE FROM notification
                WHERE id = %s AND user_id = %s AND workspace_id = %s
                RETURNING id
                """,
                (notification_id, actor.user_id, actor.workspace_id),
            )
            row = await result.fetchone()
    if not row:
        raise HTTPException(status_code=404, detail="Notification not found")
    return {"id": row["id"], "deleted": True}
