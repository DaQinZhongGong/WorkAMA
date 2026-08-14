"""PWA Web Push 服务端集成 (push_notification)。

v7.165: 移动端 PWA Web Push 订阅管理与发送。

提供：
- 4 个 REST 端点（订阅 / 发送 / 列表 / 删除）
- 兼容现有移动端 PWA 的 ``POST /api/v1/push/subscriptions`` 与
  ``POST /api/v1/push/subscriptions/remove`` 路径
- 推送投递使用 httpx POST，失败静默

设计文档：910-进度追踪与任务清单.md「PWA Web Push 服务端集成」
"""
from __future__ import annotations

import logging
from typing import Annotated, Any

import httpx
from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel, Field

from workama_platform.core import Actor, get_actor, new_id, pool

logger = logging.getLogger("workama_platform.push")

router = APIRouter(prefix="/api/v1/push", tags=["push"])


# ============================================================================
# Pydantic 数据模型
# ============================================================================


class SubscriptionCreateRequest(BaseModel):
    """创建推送订阅请求。"""

    endpoint: str = Field(min_length=1, max_length=2000)
    keys: dict[str, str] = Field(default_factory=dict)


class PushSendRequest(BaseModel):
    """发送推送请求（admin/owner）。"""

    user_id: str | None = Field(default=None, min_length=1, max_length=200)
    title: str = Field(min_length=1, max_length=200)
    body: str = Field(default="", max_length=500)
    icon: str | None = Field(default=None, max_length=500)
    badge: str | None = Field(default=None, max_length=500)
    tag: str | None = Field(default=None, max_length=200)
    url: str | None = Field(default=None, max_length=500)


class PushSubscription(BaseModel):
    """推送订阅响应。"""

    id: str
    workspace_id: str
    user_id: str
    endpoint: str
    p256dh: str | None = None
    auth: str | None = None
    created_at: str | None = None


# ============================================================================
# 辅助函数
# ============================================================================


def _require_admin(actor: Actor) -> None:
    if actor.role not in {"owner", "admin"}:
        raise HTTPException(status_code=403, detail="Admin role required")


def _subscription_summary(row: dict[str, Any]) -> dict[str, Any]:
    return {
        "id": row["id"],
        "workspace_id": row["workspace_id"],
        "user_id": row["user_id"],
        "endpoint": row["endpoint"],
        "p256dh": row.get("p256dh"),
        "auth": row.get("auth"),
        "created_at": row.get("created_at"),
    }


async def _send_push_to_endpoint(endpoint: str, payload: dict[str, Any]) -> bool:
    """向推送 endpoint 发送投递请求，失败静默并返回是否成功。"""
    try:
        async with httpx.AsyncClient(timeout=10.0) as client:
            response = await client.post(
                endpoint,
                json=payload,
                headers={"Content-Type": "application/json"},
            )
            return response.status_code < 300
    except Exception as exc:  # noqa: BLE001
        logger.debug("Push delivery failed: %s", exc)
        return False


# ============================================================================
# Router 端点
# ============================================================================


@router.post("/subscribe", status_code=status.HTTP_201_CREATED)
@router.post("/subscriptions", status_code=status.HTTP_201_CREATED)
async def subscribe(
    body: SubscriptionCreateRequest,
    actor: Annotated[Actor, Depends(get_actor)],
):
    """订阅 Web Push。

    接收 endpoint + keys(p256dh, auth)，幂等写入 ``push_subscription`` 表。
    同一 endpoint 在同一 workspace 内只保留一条记录。
    """
    p256dh = body.keys.get("p256dh", "")
    auth_key = body.keys.get("auth", "")
    sub_id = new_id("push")
    async with pool.connection() as conn:
        async with conn.transaction():
            # 幂等：同一 endpoint 在同一 workspace 先删除旧记录
            await conn.execute(
                "DELETE FROM push_subscription WHERE endpoint = %s AND workspace_id = %s",
                (body.endpoint, actor.workspace_id),
            )
            result = await conn.execute(
                """
                INSERT INTO push_subscription(
                    id, workspace_id, user_id, endpoint, p256dh, auth, created_at)
                VALUES (%s, %s, %s, %s, %s, %s, now())
                RETURNING *
                """,
                (sub_id, actor.workspace_id, actor.user_id, body.endpoint, p256dh, auth_key),
            )
            row = await result.fetchone()
    return _subscription_summary(row)


@router.post("/send")
async def send_push(
    body: PushSendRequest,
    actor: Annotated[Actor, Depends(get_actor)],
):
    """向指定 user 或 workspace 所有订阅者发送推送（admin/owner）。"""
    _require_admin(actor)
    payload = {
        "title": body.title,
        "body": body.body,
        "icon": body.icon,
        "badge": body.badge,
        "tag": body.tag,
        "url": body.url,
    }
    async with pool.connection() as conn:
        if body.user_id:
            result = await conn.execute(
                """
                SELECT endpoint FROM push_subscription
                WHERE workspace_id = %s AND user_id = %s
                """,
                (actor.workspace_id, body.user_id),
            )
        else:
            result = await conn.execute(
                """
                SELECT endpoint FROM push_subscription
                WHERE workspace_id = %s
                """,
                (actor.workspace_id,),
            )
        rows = await result.fetchall()

    sent = 0
    failed = 0
    for row in rows:
        success = await _send_push_to_endpoint(row["endpoint"], payload)
        if success:
            sent += 1
        else:
            failed += 1

    return {
        "sent": sent,
        "failed": failed,
        "total": len(rows),
        "target": body.user_id or "workspace",
    }


@router.get("/subscriptions")
async def list_subscriptions(
    actor: Annotated[Actor, Depends(get_actor)],
):
    """列表当前用户的推送订阅。"""
    async with pool.connection() as conn:
        result = await conn.execute(
            """
            SELECT * FROM push_subscription
            WHERE user_id = %s AND workspace_id = %s
            ORDER BY created_at DESC
            """,
            (actor.user_id, actor.workspace_id),
        )
        rows = await result.fetchall()
    items = [_subscription_summary(row) for row in rows]
    return {"items": items, "data": items, "count": len(items)}


@router.delete("/subscriptions/{subscription_id}", status_code=status.HTTP_200_OK)
async def delete_subscription(
    subscription_id: str,
    actor: Annotated[Actor, Depends(get_actor)],
):
    """删除指定的推送订阅。

    仅允许删除当前用户自己的订阅；owner/admin 可删除同 workspace 任意订阅。
    """
    async with pool.connection() as conn:
        async with conn.transaction():
            result = await conn.execute(
                "SELECT * FROM push_subscription WHERE id = %s",
                (subscription_id,),
            )
            row = await result.fetchone()
            if not row:
                raise HTTPException(status_code=404, detail="Subscription not found")
            if row["workspace_id"] != actor.workspace_id:
                raise HTTPException(
                    status_code=403, detail="Subscription belongs to another workspace"
                )
            if row["user_id"] != actor.user_id and actor.role not in {"owner", "admin"}:
                raise HTTPException(
                    status_code=403, detail="Cannot delete another user's subscription"
                )
            await conn.execute(
                "DELETE FROM push_subscription WHERE id = %s RETURNING id",
                (subscription_id,),
            )
    return {"id": subscription_id, "deleted": True}


@router.post("/subscriptions/remove", status_code=status.HTTP_200_OK)
async def remove_own_subscription(
    actor: Annotated[Actor, Depends(get_actor)],
):
    """删除当前用户在本 workspace 的全部推送订阅（兼容移动端 PWA）。"""
    async with pool.connection() as conn:
        async with conn.transaction():
            result = await conn.execute(
                """
                DELETE FROM push_subscription
                WHERE user_id = %s AND workspace_id = %s
                RETURNING id
                """,
                (actor.user_id, actor.workspace_id),
            )
            rows = await result.fetchall()
    return {"deleted": len(rows), "ids": [row["id"] for row in rows]}
