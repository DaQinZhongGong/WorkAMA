"""Admin 仪表盘统计聚合端点（v7.264）。

/admin 首页（AdminDashboardPage）需要各模块真实计数（工作区/智能助手/知识库/
设备/未读通知/当前套餐），此前前端调用 /api/v1/admin/stats 返回 404——这是
Lighthouse Best Practices 96 分的扣分项（errors-in-console）。本模块聚合
现有表的真实计数，全部数据来自数据库，不做任何 mock。
"""
from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Depends

from workama_platform.core import Actor, get_actor, pool

router = APIRouter(prefix="/api/v1/admin", tags=["admin"])


def _require_admin(actor: Actor) -> None:
    if actor.role not in {"owner", "admin"}:
        from fastapi import HTTPException, status

        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="admin role required")


@router.get("/stats")
async def admin_stats(actor: Annotated[Actor, Depends(get_actor)]):
    """聚合各模块真实计数，供 /admin 首页 KPI 卡片展示。"""
    _require_admin(actor)

    async def _count(sql: str, params: tuple = ()) -> int:
        result = await conn.execute(sql, params)
        row = await result.fetchone()
        return int(row["count"]) if row and row["count"] is not None else 0

    async with pool.connection() as conn:
        workspaces = await _count(
            "SELECT count(*) AS count FROM id_workspace WHERE id = %s", (actor.workspace_id,)
        )
        assistants = await _count(
            "SELECT count(*) AS count FROM assistant WHERE workspace_id = %s AND status = 'active'",
            (actor.workspace_id,),
        )
        knowledge_bases = await _count(
            "SELECT count(*) AS count FROM knowledge_base WHERE workspace_id = %s",
            (actor.workspace_id,),
        )
        devices = await _count(
            "SELECT count(*) AS count FROM id_passkey WHERE user_id = %s", (actor.user_id,)
        )
        unread_notifications = await _count(
            "SELECT count(*) AS count FROM id_notification WHERE workspace_id = %s AND read_at IS NULL",
            (actor.workspace_id,),
        )
        plan_result = await conn.execute(
            """SELECT name FROM billing_plan
               WHERE status = 'active'
               ORDER BY created_at DESC LIMIT 1"""
        )
        plan_row = await plan_result.fetchone()
        current_plan = str(plan_row["name"]) if plan_row else "community"

    return {
        "workspaces": workspaces,
        "assistants": assistants,
        "knowledge_bases": knowledge_bases,
        "devices": devices,
        "unread_notifications": unread_notifications,
        "current_plan": current_plan,
    }
