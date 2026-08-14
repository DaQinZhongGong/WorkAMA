from __future__ import annotations

import re
from typing import Annotated, Literal

from fastapi import APIRouter, Depends, HTTPException, Query, Response
from pydantic import BaseModel, Field

from workama_platform.core import Actor, get_actor, pool
from workama_platform.modules.notification.service import preference_change_allowed


router = APIRouter(prefix="/api/v1", tags=["notifications"])
_EVENT_TYPE_RE = re.compile(r"^(?:\*|[a-z0-9]+(?:[._:-][a-z0-9]+)*)$")


class NotificationPreferenceUpsert(BaseModel):
    event_type: str = Field(default="*", min_length=1, max_length=120)
    channel: Literal["in_app", "email", "webhook"]
    enabled: bool = True
    quiet_start: str | None = Field(default=None, pattern=r"^\d{2}:\d{2}$")
    quiet_end: str | None = Field(default=None, pattern=r"^\d{2}:\d{2}$")


def _notification_not_found() -> HTTPException:
    return HTTPException(status_code=404, detail="Notification not found")


def _validate_event_type(value: str) -> str:
    normalized = value.strip().lower()
    if not _EVENT_TYPE_RE.fullmatch(normalized):
        raise HTTPException(status_code=422, detail="Notification event_type is invalid")
    return normalized


@router.get("/notifications")
async def list_notifications(
    actor: Annotated[Actor, Depends(get_actor)],
    unread_only: bool = Query(default=False),
    limit: int = Query(default=50, ge=1, le=100),
):
    async with pool.connection() as conn:
        result = await conn.execute(
            """
            SELECT id, event_type, priority, title, summary, action_url, payload_min,
                   resource_ref, read_at, created_at, expires_at
            FROM id_notification
            WHERE user_id = %s AND workspace_id = %s AND archived_at IS NULL
              AND (expires_at IS NULL OR expires_at > now())
              AND (%s = FALSE OR read_at IS NULL)
            ORDER BY created_at DESC, id DESC LIMIT %s
            """,
            (actor.user_id, actor.workspace_id, unread_only, limit),
        )
        count = await conn.execute(
            """
            SELECT count(*) AS count
            FROM id_notification
            WHERE user_id = %s AND workspace_id = %s AND archived_at IS NULL
              AND (expires_at IS NULL OR expires_at > now()) AND read_at IS NULL
            """,
            (actor.user_id, actor.workspace_id),
        )
        items = await result.fetchall()
        unread_count = (await count.fetchone())["count"]
    return {"items": items, "data": items, "next_cursor": None, "has_more": False, "meta": {"request_id": None}, "unread_count": unread_count}


@router.get("/notifications/{notification_id}")
async def get_notification(notification_id: str, actor: Annotated[Actor, Depends(get_actor)]):
    async with pool.connection() as conn:
        result = await conn.execute(
            """
            SELECT id, event_type, priority, title, summary, action_url, payload_min,
                   resource_ref, read_at, archived_at, expires_at, created_at
            FROM id_notification
            WHERE id=%s AND user_id=%s AND workspace_id=%s
              AND (expires_at IS NULL OR expires_at > now())
            """,
            (notification_id, actor.user_id, actor.workspace_id),
        )
        notification = await result.fetchone()
        if not notification:
            raise _notification_not_found()
        deliveries = await conn.execute(
            """
            SELECT channel, provider, attempt, status, provider_id, error_class,
                   next_attempt_at, created_at, updated_at
            FROM id_notification_delivery
            WHERE notification_id=%s
            ORDER BY channel
            """,
            (notification_id,),
        )
        notification["deliveries"] = await deliveries.fetchall()
    return notification


@router.post("/notifications/{notification_id}/read-receipts")
async def mark_read(notification_id: str, actor: Annotated[Actor, Depends(get_actor)]):
    async with pool.connection() as conn:
        result = await conn.execute(
            """
            UPDATE id_notification
            SET read_at = COALESCE(read_at, now())
            WHERE id = %s AND user_id = %s AND workspace_id = %s
            RETURNING id, read_at
            """,
            (notification_id, actor.user_id, actor.workspace_id),
        )
        row = await result.fetchone()
        if not row:
            raise _notification_not_found()
        await conn.commit()
    return row


@router.post("/notification-read-receipts")
async def mark_all_read(actor: Annotated[Actor, Depends(get_actor)]):
    async with pool.connection() as conn:
        result = await conn.execute(
            """
            UPDATE id_notification
            SET read_at = now()
            WHERE user_id = %s AND workspace_id = %s AND archived_at IS NULL
              AND (expires_at IS NULL OR expires_at > now()) AND read_at IS NULL
            """,
            (actor.user_id, actor.workspace_id),
        )
        await conn.commit()
    return {"updated": result.rowcount}


@router.delete("/notifications/{notification_id}", status_code=204)
async def archive(notification_id: str, actor: Annotated[Actor, Depends(get_actor)]):
    async with pool.connection() as conn:
        result = await conn.execute(
            """
            UPDATE id_notification SET archived_at = now()
            WHERE id = %s AND user_id = %s AND workspace_id = %s
            RETURNING id
            """,
            (notification_id, actor.user_id, actor.workspace_id),
        )
        if not await result.fetchone():
            raise _notification_not_found()
        await conn.commit()
    return Response(status_code=204)


@router.get("/notification-preferences")
async def get_notification_preferences(actor: Annotated[Actor, Depends(get_actor)]):
    async with pool.connection() as conn:
        result = await conn.execute(
            """
            SELECT event_type, channel, enabled, quiet_start, quiet_end, updated_at
            FROM id_notification_preference
            WHERE user_id=%s AND workspace_id=%s
            ORDER BY event_type, channel
            """,
            (actor.user_id, actor.workspace_id),
        )
        rows = await result.fetchall()
    return {"items": rows, "data": rows, "next_cursor": None, "has_more": False, "meta": {"request_id": None}}


@router.put("/notification-preferences")
async def update_notification_preferences(
    body: NotificationPreferenceUpsert,
    actor: Annotated[Actor, Depends(get_actor)],
):
    event_type = _validate_event_type(body.event_type)
    if not preference_change_allowed(event_type, body.channel, body.enabled):
        raise HTTPException(
            status_code=409,
            detail="Security and billing in-app notifications cannot be disabled",
        )
    async with pool.connection() as conn:
        result = await conn.execute(
            """
            INSERT INTO id_notification_preference(
                id, user_id, workspace_id, event_type, channel, enabled,
                quiet_start, quiet_end, updated_at
            ) VALUES (%s,%s,%s,%s,%s,%s,%s::time,%s::time,now())
            ON CONFLICT(user_id, workspace_id, event_type, channel) DO UPDATE SET
                enabled=EXCLUDED.enabled, quiet_start=EXCLUDED.quiet_start,
                quiet_end=EXCLUDED.quiet_end, updated_at=now()
            RETURNING event_type, channel, enabled, quiet_start, quiet_end, updated_at
            """,
            (
                f"pref_{actor.user_id}_{actor.workspace_id}_{event_type}_{body.channel}",
                actor.user_id,
                actor.workspace_id,
                event_type,
                body.channel,
                body.enabled,
                body.quiet_start,
                body.quiet_end,
            ),
        )
        row = await result.fetchone()
        await conn.commit()
    return row
