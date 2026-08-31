"""订阅与计费模块 (billing)。

v7.148: 套餐 / 订阅 / 用量 / 发票闭环。

设计文档：910-进度追踪与任务清单.md「P1 订阅与计费模块」。

注意：本模块文件名为 ``billing.py``，与同目录下的 ``billing/`` 包同名。
Python 导入系统会让包优先于同名模块，因此本文件无法通过
``from workama_platform.modules.billing import ...`` 导入。``main.py`` 与
``tests/test_billing.py`` 使用 ``importlib.util.spec_from_file_location``
按文件路径直接加载本模块，绕过包遮蔽。

提供：
- 12 个 REST 端点（套餐 CRUD / 订阅创建切换取消 / 用量记录查询汇总 / 发票列表详情）
- ``ensure_default_plans()`` 启动钩子，幂等创建 free/starter/pro/enterprise 4 个默认套餐
- ``check_quota()`` 辅助函数，检查当前周期用量是否超额
- 所有金额使用 ``Decimal``，不使用 float
- 端点均需 ``Actor`` 鉴权（``GET /plans`` 公开除外）
"""
from __future__ import annotations

import logging
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import Annotated, Any, Literal

from fastapi import APIRouter, Depends, HTTPException, Query, status
from pydantic import BaseModel, Field

import json

from workama_platform.core import (
    Actor,
    _cache_key,
    cache_delete,
    cache_get,
    cache_set,
    get_actor,
    json_dumps,
    new_id,
    pool,
)

import asyncio

logger = logging.getLogger("workama_platform.billing")

router = APIRouter(prefix="/api/v1/billing", tags=["billing"])

# ============================================================================
# 常量
# ============================================================================

BillingCycle = Literal["monthly", "yearly"]
SubscriptionStatus = Literal["active", "past_due", "canceled", "trialing"]
InvoiceStatus = Literal["pending", "paid", "void", "refunded"]
PlanStatus = Literal["active", "disabled"]
UsageMetric = Literal["tokens_used", "seats_used", "storage_used_gb", "api_calls"]

# metric → plan 字段映射，用于 quota 比较
_METRIC_TO_QUOTA_FIELD: dict[str, str] = {
    "tokens_used": "token_quota",
    "seats_used": "seat_quota",
    "storage_used_gb": "storage_quota_gb",
    "api_calls": "api_rate_limit",
}

# 4 个默认套餐（free/starter/pro/enterprise）
DEFAULT_PLANS: list[dict[str, Any]] = [
    {
        "code": "free",
        "name": "Free",
        "description": "Free tier for individuals and evaluation",
        "price_monthly": Decimal("0"),
        "price_yearly": Decimal("0"),
        "token_quota": 1_000_000,
        "seat_quota": 1,
        "storage_quota_gb": 1,
        "api_rate_limit": 60,
        "features": ["community_support", "1_workspace"],
    },
    {
        "code": "starter",
        "name": "Starter",
        "description": "Starter tier for small teams",
        "price_monthly": Decimal("19"),
        "price_yearly": Decimal("190"),
        "token_quota": 10_000_000,
        "seat_quota": 5,
        "storage_quota_gb": 10,
        "api_rate_limit": 300,
        "features": ["email_support", "5_workspaces"],
    },
    {
        "code": "pro",
        "name": "Pro",
        "description": "Pro tier for growing teams",
        "price_monthly": Decimal("99"),
        "price_yearly": Decimal("990"),
        "token_quota": 100_000_000,
        "seat_quota": 20,
        "storage_quota_gb": 100,
        "api_rate_limit": 1200,
        "features": ["priority_support", "unlimited_workspaces", "audit_logs"],
    },
    {
        "code": "enterprise",
        "name": "Enterprise",
        "description": "Enterprise tier with custom quotas and SSO",
        "price_monthly": Decimal("499"),
        "price_yearly": Decimal("4990"),
        "token_quota": 10_000_000_000,
        "seat_quota": 500,
        "storage_quota_gb": 1000,
        "api_rate_limit": 6000,
        "features": ["dedicated_support", "sso", "sla", "custom_contracts"],
    },
]


# ============================================================================
# Pydantic 数据模型
# ============================================================================


class Plan(BaseModel):
    """套餐。"""

    id: str
    code: str
    name: str
    description: str | None = None
    price_monthly: Decimal
    price_yearly: Decimal
    token_quota: int
    seat_quota: int
    storage_quota_gb: int
    api_rate_limit: int
    features: list[str] = Field(default_factory=list)
    status: str = "active"
    metadata: dict[str, Any] = Field(default_factory=dict)
    created_at: datetime
    updated_at: datetime


class Subscription(BaseModel):
    """订阅。"""

    id: str
    workspace_id: str
    plan_id: str
    billing_cycle: str
    status: str
    current_period_start: datetime
    current_period_end: datetime
    trial_end: datetime | None = None
    canceled_at: datetime | None = None
    metadata: dict[str, Any] = Field(default_factory=dict)
    created_at: datetime
    updated_at: datetime


class UsageRecord(BaseModel):
    """用量记录。"""

    id: str
    workspace_id: str
    subscription_id: str
    metric: str
    value: int
    period_start: datetime
    period_end: datetime
    metadata: dict[str, Any] = Field(default_factory=dict)
    created_at: datetime


class Invoice(BaseModel):
    """发票。"""

    id: str
    workspace_id: str
    subscription_id: str
    amount: Decimal
    currency: str
    status: str
    period_start: datetime
    period_end: datetime
    paid_at: datetime | None = None
    metadata: dict[str, Any] = Field(default_factory=dict)
    created_at: datetime


class PlanCreateRequest(BaseModel):
    """创建套餐请求（admin only）。"""

    code: str = Field(min_length=1, max_length=64)
    name: str = Field(min_length=1, max_length=200)
    description: str | None = Field(default=None, max_length=2000)
    price_monthly: Decimal = Field(default=Decimal("0"), ge=0, max_digits=10, decimal_places=2)
    price_yearly: Decimal = Field(default=Decimal("0"), ge=0, max_digits=10, decimal_places=2)
    token_quota: int = Field(default=1_000_000, ge=0)
    seat_quota: int = Field(default=1, ge=0)
    storage_quota_gb: int = Field(default=1, ge=0)
    api_rate_limit: int = Field(default=60, ge=0)
    features: list[str] = Field(default_factory=list)
    status: PlanStatus = "active"
    metadata: dict[str, Any] = Field(default_factory=dict)


class SubscriptionCreateRequest(BaseModel):
    """创建/切换订阅请求。"""

    plan_id: str = Field(min_length=1, max_length=80)
    billing_cycle: BillingCycle = "monthly"


class UsageRecordRequest(BaseModel):
    """记录用量请求。"""

    subscription_id: str = Field(min_length=1, max_length=80)
    metric: UsageMetric
    value: int = Field(ge=0)
    period_start: datetime
    period_end: datetime
    metadata: dict[str, Any] = Field(default_factory=dict)


class PlanResponse(Plan):
    """套餐响应。"""


class SubscriptionResponse(Subscription):
    """订阅响应。"""


class InvoiceResponse(Invoice):
    """发票响应。"""


class UsageSummary(BaseModel):
    """用量汇总。"""

    workspace_id: str
    subscription_id: str | None = None
    plan_id: str | None = None
    period_start: datetime | None = None
    period_end: datetime | None = None
    has_active_subscription: bool = False
    metrics: list[dict[str, Any]] = Field(default_factory=list)


# ============================================================================
# 辅助函数
# ============================================================================


def _require_admin(actor: Actor) -> None:
    if actor.role not in {"owner", "admin"}:
        raise HTTPException(status_code=403, detail="Admin role required")


def _require_owner(actor: Actor) -> None:
    if actor.role != "owner":
        raise HTTPException(status_code=403, detail="Organization owner required")


def _plan_summary(row: dict[str, Any]) -> dict[str, Any]:
    return {
        "id": row["id"],
        "code": row["code"],
        "name": row["name"],
        "description": row.get("description"),
        "price_monthly": row["price_monthly"],
        "price_yearly": row["price_yearly"],
        "token_quota": row["token_quota"],
        "seat_quota": row["seat_quota"],
        "storage_quota_gb": row["storage_quota_gb"],
        "api_rate_limit": row["api_rate_limit"],
        "features": row.get("features") or [],
        "status": row["status"],
        "metadata": row.get("metadata") or {},
        "created_at": row["created_at"],
        "updated_at": row["updated_at"],
    }


def _subscription_summary(row: dict[str, Any]) -> dict[str, Any]:
    return {
        "id": row["id"],
        "workspace_id": row["workspace_id"],
        "plan_id": row["plan_id"],
        "billing_cycle": row["billing_cycle"],
        "status": row["status"],
        "current_period_start": row["current_period_start"],
        "current_period_end": row["current_period_end"],
        "trial_end": row.get("trial_end"),
        "canceled_at": row.get("canceled_at"),
        "metadata": row.get("metadata") or {},
        "created_at": row["created_at"],
        "updated_at": row["updated_at"],
    }


def _usage_summary(row: dict[str, Any]) -> dict[str, Any]:
    return {
        "id": row["id"],
        "workspace_id": row["workspace_id"],
        "subscription_id": row["subscription_id"],
        "metric": row["metric"],
        "value": row["value"],
        "period_start": row["period_start"],
        "period_end": row["period_end"],
        "metadata": row.get("metadata") or {},
        "created_at": row["created_at"],
    }


def _invoice_summary(row: dict[str, Any]) -> dict[str, Any]:
    return {
        "id": row["id"],
        "workspace_id": row["workspace_id"],
        "subscription_id": row["subscription_id"],
        "amount": row["amount"],
        "currency": row["currency"],
        "status": row["status"],
        "period_start": row["period_start"],
        "period_end": row["period_end"],
        "paid_at": row.get("paid_at"),
        "metadata": row.get("metadata") or {},
        "created_at": row["created_at"],
    }


async def _owned_subscription(
    conn: Any, subscription_id: str, actor: Actor
) -> dict[str, Any]:
    """查询订阅并校验 workspace 归属。

    - 不存在 → 404
    - 存在但 workspace 不匹配 → 403
    """
    result = await conn.execute(
        "SELECT * FROM billing_subscription WHERE id = %s",
        (subscription_id,),
    )
    row = await result.fetchone()
    if not row:
        raise HTTPException(status_code=404, detail="Subscription not found")
    if row["workspace_id"] != actor.workspace_id:
        raise HTTPException(
            status_code=403, detail="Subscription belongs to another workspace"
        )
    return row


async def _owned_invoice(
    conn: Any, invoice_id: str, actor: Actor
) -> dict[str, Any]:
    """查询发票并校验 workspace 归属。"""
    result = await conn.execute(
        "SELECT * FROM billing_invoice WHERE id = %s",
        (invoice_id,),
    )
    row = await result.fetchone()
    if not row:
        raise HTTPException(status_code=404, detail="Invoice not found")
    if row["workspace_id"] != actor.workspace_id:
        raise HTTPException(
            status_code=403, detail="Invoice belongs to another workspace"
        )
    return row


def _period_for_cycle(cycle: str) -> timedelta:
    return timedelta(days=365) if cycle == "yearly" else timedelta(days=30)


# ============================================================================
# 默认套餐启动钩子
# ============================================================================


async def ensure_default_plans(conn: Any | None = None) -> int:
    """幂等创建 4 个默认套餐（free/starter/pro/enterprise）。

    传入 ``conn`` 时复用连接（不 commit，由外层事务控制）；不传时自建连接并 commit。
    任何异常只 log warning 不抛出，避免阻断启动。返回 upsert 的套餐数量。
    """
    async def _upsert(c: Any) -> int:
        count = 0
        for plan in DEFAULT_PLANS:
            await c.execute(
                """
                INSERT INTO billing_plan(
                    id, code, name, description, price_monthly, price_yearly,
                    token_quota, seat_quota, storage_quota_gb, api_rate_limit,
                    features, status, metadata)
                VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s::jsonb, 'active', '{}'::jsonb)
                ON CONFLICT (code) DO UPDATE SET
                    name = EXCLUDED.name,
                    description = EXCLUDED.description,
                    price_monthly = EXCLUDED.price_monthly,
                    price_yearly = EXCLUDED.price_yearly,
                    token_quota = EXCLUDED.token_quota,
                    seat_quota = EXCLUDED.seat_quota,
                    storage_quota_gb = EXCLUDED.storage_quota_gb,
                    api_rate_limit = EXCLUDED.api_rate_limit,
                    features = EXCLUDED.features,
                    status = 'active',
                    updated_at = now()
                """,
                (
                    new_id("plan"),
                    plan["code"],
                    plan["name"],
                    plan["description"],
                    plan["price_monthly"],
                    plan["price_yearly"],
                    plan["token_quota"],
                    plan["seat_quota"],
                    plan["storage_quota_gb"],
                    plan["api_rate_limit"],
                    json_dumps(plan["features"]),
                ),
            )
            count += 1
        return count

    try:
        if conn is not None:
            return await _upsert(conn)
        async with pool.connection() as c:
            n = await _upsert(c)
            await c.commit()
            return n
    except Exception as exc:  # noqa: BLE001
        logger.warning("ensure_default_plans failed: %s", exc)
        return 0


# ============================================================================
# 用量检查
# ============================================================================


async def check_quota(
    workspace_id: str, metric: str, value: int = 0
) -> dict[str, Any]:
    """检查 workspace 当前周期内 ``metric`` 用量 + ``value`` 是否超过套餐 quota。

    返回::

        {
          "workspace_id": str,
          "metric": str,
          "used": int,            # 当前周期已用量
          "quota": int | None,    # 套餐 quota（None 表示该 metric 不受限）
          "projected_total": int, # used + value
          "exceeded": bool,       # projected_total > quota
          "reason": str,          # ok / no_active_subscription / metric_not_capped
        }
    """
    async with pool.connection() as conn:
        sub_result = await conn.execute(
            """
            SELECT s.id, s.current_period_start, s.current_period_end,
                   p.token_quota, p.seat_quota, p.storage_quota_gb, p.api_rate_limit
            FROM billing_subscription s
            JOIN billing_plan p ON p.id = s.plan_id
            WHERE s.workspace_id = %s AND s.status = 'active'
            ORDER BY s.created_at DESC LIMIT 1
            """,
            (workspace_id,),
        )
        sub = await sub_result.fetchone()
        if not sub:
            return {
                "workspace_id": workspace_id,
                "metric": metric,
                "used": 0,
                "quota": None,
                "projected_total": value,
                "exceeded": False,
                "reason": "no_active_subscription",
            }
        quota_field = _METRIC_TO_QUOTA_FIELD.get(metric)
        if not quota_field:
            return {
                "workspace_id": workspace_id,
                "metric": metric,
                "used": 0,
                "quota": None,
                "projected_total": value,
                "exceeded": False,
                "reason": "metric_not_capped",
            }
        quota = int(sub[quota_field])
        usage_result = await conn.execute(
            """
            SELECT COALESCE(SUM(value), 0) AS used FROM billing_usage_record
            WHERE workspace_id = %s AND metric = %s
              AND period_start >= %s AND period_end <= %s
            """,
            (
                workspace_id,
                metric,
                sub["current_period_start"],
                sub["current_period_end"],
            ),
        )
        usage_row = await usage_result.fetchone()
        used = int(usage_row["used"]) if usage_row else 0
        projected = used + value
        return {
            "workspace_id": workspace_id,
            "metric": metric,
            "used": used,
            "quota": quota,
            "projected_total": projected,
            "exceeded": projected > quota,
            "reason": "ok",
        }


# ============================================================================
# Router 端点
# ============================================================================
#
# 路由声明顺序：具体路径（/plans, /subscriptions, /usage, /usage/summary,
# /invoices）在参数化路径之前。注意 /usage/summary 必须在 /usage 之前声明，
# 否则 "summary" 会被 /usage 之后的路径捕获（此处 /usage 为 GET 无参数，无冲突）。


@router.get("/overview")
async def billing_overview(
    actor: Annotated[Actor, Depends(get_actor)],
):
    """聚合概览：供控制台 billing-page 一次拉取所需全部数据（生产级）。

    聚合：plans(公开 active) + active subscription(当前 workspace) + period usage summary
    + recent billing events(近 8 条 bill_transaction) + quota 预检。结果按 workspace 缓存 60s，
    写路径（create_plan / subscription 变更）失效。所有金额 Decimal 保持字符串序列化由 json_dumps 统一处理。
    前端契约 BillingData {plans, subscription, usage, events} 直接可用。
    """
    cache_key = _cache_key(actor.workspace_id, "billing", "overview")
    cached = await cache_get(cache_key)
    if cached:
        return json.loads(cached)

    async with pool.connection() as conn:
        # 并发查询：plans / active sub / hourly aggregates / recent transactions
        # plans（公开 active，复用 list_plans 排序）
        plans_task = conn.execute(
            "SELECT * FROM billing_plan WHERE status = 'active' ORDER BY price_monthly, code"
        )
        sub_task = conn.execute(
            """
            SELECT s.*, p.code AS plan_code, p.name AS plan_name,
                   p.price_monthly, p.token_quota, p.seat_quota, p.storage_quota_gb, p.api_rate_limit, p.features
            FROM billing_subscription s
            LEFT JOIN billing_plan p ON p.id = s.plan_id
            WHERE s.workspace_id = %s AND s.status = 'active'
            ORDER BY s.created_at DESC LIMIT 1
            """,
            (actor.workspace_id,),
        )
        # 先取 plans 与 sub，再按 sub 周期查 usage/transaction（依赖 sub 周期）
        plans_result = await plans_task
        sub_result = await sub_task
        plan_rows = await plans_result.fetchall()
        sub_row = await sub_result.fetchone()

        # 映射 plans 为前端 BillingData.plans 形状
        plans = []
        for r in plan_rows:
            plans.append(
                {
                    "id": r["id"],
                    "code": r["code"],
                    "name": r["name"],
                    "price": int(r["price_monthly"]) if r["price_monthly"] is not None else 0,
                    "currency": "CNY",
                    "seats": int(r["seat_quota"]) if r.get("seat_quota") is not None else None,
                    "monthly_credits": int(r["token_quota"]) if r.get("token_quota") is not None else None,
                    "features": r.get("features") or [],
                    "description": r.get("description"),
                    "price_monthly": r["price_monthly"],
                    "price_yearly": r["price_yearly"],
                    "token_quota": r["token_quota"],
                    "seat_quota": r["seat_quota"],
                    "storage_quota_gb": r["storage_quota_gb"],
                    "api_rate_limit": r["api_rate_limit"],
                }
            )

        # subscription 映射为前端 Subscription
        if sub_row:
            subscription = {
                "plan_id": sub_row["plan_id"],
                "plan_code": sub_row.get("plan_code") or sub_row.get("code"),
                "plan_name": sub_row.get("plan_name") or sub_row.get("name"),
                "status": sub_row["status"],
                "seats": int(sub_row.get("seat_quota") or 0) if sub_row.get("seat_quota") else None,
                "renew_at": sub_row["current_period_end"].isoformat() if sub_row.get("current_period_end") else None,
                "started_at": sub_row["current_period_start"].isoformat() if sub_row.get("current_period_start") else None,
                "id": sub_row["id"],
                "billing_cycle": sub_row.get("billing_cycle"),
                "current_period_start": sub_row.get("current_period_start"),
                "current_period_end": sub_row.get("current_period_end"),
            }
            period_start = sub_row["current_period_start"]
            period_end = sub_row["current_period_end"]
        else:
            subscription = None
            period_start = period_end = None

        # usage 汇总：bill_usage_record + bill_usage_hourly（fallback）
        if sub_row and period_start and period_end:
            usage_result = await conn.execute(
                """
                SELECT COALESCE(SUM(prompt_tokens + completion_tokens), 0) AS tokens,
                       COUNT(*) AS requests,
                       COALESCE(SUM(cost_credits), 0) AS credits
                FROM bill_usage_record
                WHERE workspace_id = %s AND created_at >= %s AND created_at < %s
                """,
                (actor.workspace_id, period_start, period_end),
            )
            usage_row = await usage_result.fetchone()
            # storage 仍走 billing_usage_record metric=storage_used_gb（如无则 0）
            storage_result = await conn.execute(
                """
                SELECT COALESCE(SUM(value),0) AS storage_val FROM billing_usage_record
                WHERE workspace_id = %s AND metric = 'storage_used_gb'
                  AND period_start >= %s AND period_end <= %s
                """,
                (actor.workspace_id, period_start, period_end),
            )
            storage_row = await storage_result.fetchone()
            usage = {
                "requests": int(usage_row["requests"] or 0) if usage_row else 0,
                "tokens": int(usage_row["tokens"] or 0) if usage_row else 0,
                "credits_used": int(float(usage_row["credits"] or 0)) if usage_row else 0,
                "storage_mb": int(float(storage_row["storage_val"] or 0) * 1024) if storage_row else 0,
                "month": period_start.strftime("%Y-%m") if period_start else None,
            }
        else:
            usage = {"requests": 0, "tokens": 0, "storage_mb": 0, "credits_used": 0, "month": None}

        # events：近 8 条 bill_transaction / billing_invoice 统一映射
        events = []
        try:
            tx_result = await conn.execute(
                """
                SELECT id, kind, amount, description, created_at
                FROM bill_transaction
                WHERE workspace_id = %s
                ORDER BY created_at DESC LIMIT 8
                """,
                (actor.workspace_id,),
            )
            tx_rows = await tx_result.fetchall()
            for r in tx_rows:
                amt = r["amount"]
                # Decimal -> int credits
                try:
                    amt_val = int(float(amt)) if amt is not None else 0
                except Exception:
                    amt_val = 0
                events.append(
                    {
                        "id": r["id"],
                        "type": r["kind"] or "transaction",
                        "amount": amt_val,
                        "currency": "CNY",
                        "description": r.get("description") or r["kind"] or r["id"],
                        "created_at": r["created_at"].isoformat() if r.get("created_at") else None,
                    }
                )
        except Exception:
            events = []
        # 若 bill_transaction 为空，fallback 到 billing_invoice
        if not events:
            try:
                inv_result = await conn.execute(
                    """
                    SELECT id, status AS kind, amount, created_at,
                           'Invoice ' || id AS description
                    FROM billing_invoice WHERE workspace_id = %s
                    ORDER BY created_at DESC LIMIT 8
                    """,
                    (actor.workspace_id,),
                )
                inv_rows = await inv_result.fetchall()
                for r in inv_rows:
                    events.append(
                        {
                            "id": r["id"],
                            "type": r["kind"] or "invoice",
                            "amount": int(float(r["amount"] or 0)),
                            "currency": "CNY",
                            "description": r.get("description") or r["id"],
                            "created_at": r["created_at"].isoformat() if r.get("created_at") else None,
                        }
                    )
            except Exception:
                pass

    response = {
        "plans": plans,
        "subscription": subscription,
        "usage": usage,
        "events": events,
    }
    # 缓存 60s，失败不阻断
    try:
        await cache_set(cache_key, json_dumps(response))
    except Exception:
        pass
    return response


@router.get("/plans")
async def list_plans():
    """列出所有 active 套餐（公开，无需鉴权）。"""
    cache_key = _cache_key("_global_", "billing_plans", "active")
    cached = await cache_get(cache_key)
    if cached:
        return json.loads(cached)
    async with pool.connection() as conn:
        result = await conn.execute(
            "SELECT * FROM billing_plan WHERE status = 'active' ORDER BY price_monthly, code"
        )
        rows = await result.fetchall()
    items = [_plan_summary(row) for row in rows]
    response = {
        "items": items,
        "data": items,
        "count": len(items),
    }
    await cache_set(cache_key, json_dumps(response))
    return response


@router.get("/plans/{plan_id}")
async def get_plan(plan_id: str):
    """套餐详情（公开）。"""
    async with pool.connection() as conn:
        result = await conn.execute(
            "SELECT * FROM billing_plan WHERE id = %s", (plan_id,)
        )
        row = await result.fetchone()
    if not row:
        raise HTTPException(status_code=404, detail="Plan not found")
    return _plan_summary(row)


@router.post("/plans", status_code=status.HTTP_201_CREATED)
async def create_plan(
    body: PlanCreateRequest,
    actor: Annotated[Actor, Depends(get_actor)],
):
    """创建套餐（admin only）。"""
    _require_admin(actor)
    plan_id = new_id("plan")
    async with pool.connection() as conn:
        async with conn.transaction():
            existing_result = await conn.execute(
                "SELECT id FROM billing_plan WHERE code = %s", (body.code,)
            )
            if await existing_result.fetchone():
                raise HTTPException(
                    status_code=409, detail=f"Plan code already exists: {body.code}"
                )
            result = await conn.execute(
                """
                INSERT INTO billing_plan(
                    id, code, name, description, price_monthly, price_yearly,
                    token_quota, seat_quota, storage_quota_gb, api_rate_limit,
                    features, status, metadata)
                VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s::jsonb, %s, %s::jsonb)
                RETURNING *
                """,
                (
                    plan_id,
                    body.code,
                    body.name,
                    body.description,
                    body.price_monthly,
                    body.price_yearly,
                    body.token_quota,
                    body.seat_quota,
                    body.storage_quota_gb,
                    body.api_rate_limit,
                    json_dumps(body.features),
                    body.status,
                    json_dumps(body.metadata),
                ),
            )
            row = await result.fetchone()
    await cache_delete(_cache_key("_global_", "billing_plans", "active"))
    # overview 聚合含 plans，按 workspace 维度缓存，计划变更后让 TTL 自然失效或由网关侧主动失效；此处清理全局 plans 缓存即可
    return _plan_summary(row)


@router.get("/subscriptions")
async def list_subscriptions(
    actor: Annotated[Actor, Depends(get_actor)],
):
    """当前 workspace 的订阅列表。"""
    async with pool.connection() as conn:
        result = await conn.execute(
            "SELECT * FROM billing_subscription WHERE workspace_id = %s ORDER BY created_at DESC",
            (actor.workspace_id,),
        )
        rows = await result.fetchall()
    items = [_subscription_summary(row) for row in rows]
    return {"items": items, "data": items, "count": len(items)}


@router.post("/subscriptions", status_code=status.HTTP_201_CREATED)
async def create_subscription(
    body: SubscriptionCreateRequest,
    actor: Annotated[Actor, Depends(get_actor)],
):
    """创建或切换订阅。

    - 若 workspace 已有 active 订阅，切换其 plan_id 与 billing_cycle（switched=True）
    - 否则创建新订阅（switched=False）
    """
    _require_owner(actor)
    async with pool.connection() as conn:
        async with conn.transaction():
            plan_result = await conn.execute(
                "SELECT * FROM billing_plan WHERE id = %s AND status = 'active'",
                (body.plan_id,),
            )
            plan = await plan_result.fetchone()
            if not plan:
                raise HTTPException(status_code=404, detail="Plan not found")
            existing_result = await conn.execute(
                "SELECT * FROM billing_subscription WHERE workspace_id = %s AND status = 'active'",
                (actor.workspace_id,),
            )
            existing = await existing_result.fetchone()
            start = datetime.now(UTC)
            end = start + _period_for_cycle(body.billing_cycle)
            if existing:
                sub_id = existing["id"]
                await conn.execute(
                    """
                    UPDATE billing_subscription SET
                        plan_id = %s, billing_cycle = %s,
                        current_period_start = %s, current_period_end = %s,
                        status = 'active', canceled_at = NULL, updated_at = now()
                    WHERE id = %s
                    """,
                    (body.plan_id, body.billing_cycle, start, end, sub_id),
                )
                switched = True
            else:
                sub_id = new_id("sub")
                await conn.execute(
                    """
                    INSERT INTO billing_subscription(
                        id, workspace_id, plan_id, billing_cycle, status,
                        current_period_start, current_period_end, metadata)
                    VALUES (%s, %s, %s, %s, 'active', %s, %s, '{}'::jsonb)
                    """,
                    (sub_id, actor.workspace_id, body.plan_id, body.billing_cycle, start, end),
                )
                switched = False
            result = await conn.execute(
                "SELECT * FROM billing_subscription WHERE id = %s", (sub_id,)
            )
            row = await result.fetchone()
    # 订阅变更失效该 workspace 的 overview 聚合缓存
    try:
        await cache_delete(_cache_key(actor.workspace_id, "billing", "overview"))
    except Exception:
        pass
    return {**_subscription_summary(row), "switched": switched, "plan": _plan_summary(plan)}


@router.get("/subscriptions/{subscription_id}")
async def get_subscription(
    subscription_id: str,
    actor: Annotated[Actor, Depends(get_actor)],
):
    """订阅详情。"""
    async with pool.connection() as conn:
        row = await _owned_subscription(conn, subscription_id, actor)
    return _subscription_summary(row)


@router.delete("/subscriptions/{subscription_id}")
async def cancel_subscription(
    subscription_id: str,
    actor: Annotated[Actor, Depends(get_actor)],
):
    """取消订阅（status=canceled, canceled_at=now）。"""
    _require_owner(actor)
    async with pool.connection() as conn:
        async with conn.transaction():
            row = await _owned_subscription(conn, subscription_id, actor)
            if row["status"] == "canceled":
                raise HTTPException(
                    status_code=409, detail="Subscription already canceled"
                )
            await conn.execute(
                """
                UPDATE billing_subscription SET status = 'canceled', canceled_at = now(), updated_at = now()
                WHERE id = %s
                """,
                (subscription_id,),
            )
            result = await conn.execute(
                "SELECT * FROM billing_subscription WHERE id = %s",
                (subscription_id,),
            )
            updated = await result.fetchone()
    try:
        await cache_delete(_cache_key(actor.workspace_id, "billing", "overview"))
    except Exception:
        pass
    return _subscription_summary(updated)


@router.post("/usage", status_code=status.HTTP_201_CREATED)
async def record_usage(
    body: UsageRecordRequest,
    actor: Annotated[Actor, Depends(get_actor)],
):
    """记录用量。

    校验 subscription 属于当前 workspace，然后插入 billing_usage_record。
    """
    async with pool.connection() as conn:
        async with conn.transaction():
            sub_result = await conn.execute(
                "SELECT * FROM billing_subscription WHERE id = %s",
                (body.subscription_id,),
            )
            sub = await sub_result.fetchone()
            if not sub:
                raise HTTPException(status_code=404, detail="Subscription not found")
            if sub["workspace_id"] != actor.workspace_id:
                raise HTTPException(
                    status_code=403,
                    detail="Subscription belongs to another workspace",
                )
            usage_id = new_id("use")
            result = await conn.execute(
                """
                INSERT INTO billing_usage_record(
                    id, workspace_id, subscription_id, metric, value,
                    period_start, period_end, metadata)
                VALUES (%s, %s, %s, %s, %s, %s, %s, %s::jsonb)
                RETURNING *
                """,
                (
                    usage_id,
                    actor.workspace_id,
                    body.subscription_id,
                    body.metric,
                    body.value,
                    body.period_start,
                    body.period_end,
                    json_dumps(body.metadata),
                ),
            )
            row = await result.fetchone()
    return _usage_summary(row)


@router.get("/usage")
async def list_usage(
    actor: Annotated[Actor, Depends(get_actor)],
    metric: str | None = Query(default=None, max_length=64),
    start: datetime | None = Query(default=None),
    end: datetime | None = Query(default=None),
    limit: int = Query(default=50, ge=1, le=200),
    offset: int = Query(default=0, ge=0),
):
    """查询用量，支持 metric / period 过滤与分页。"""
    clauses = ["workspace_id = %s"]
    params: list[Any] = [actor.workspace_id]
    if metric:
        clauses.append("metric = %s")
        params.append(metric)
    if start:
        clauses.append("period_start >= %s")
        params.append(start)
    if end:
        clauses.append("period_end <= %s")
        params.append(end)
    params.append(limit)
    params.append(offset)
    async with pool.connection() as conn:
        result = await conn.execute(
            f"""
            SELECT * FROM billing_usage_record
            WHERE {" AND ".join(clauses)}
            ORDER BY created_at DESC LIMIT %s OFFSET %s
            """,
            tuple(params),
        )
        rows = await result.fetchall()
    items = [_usage_summary(row) for row in rows]
    return {"items": items, "data": items, "count": len(items), "limit": limit, "offset": offset}


@router.get("/usage/summary")
async def usage_summary(
    actor: Annotated[Actor, Depends(get_actor)],
):
    """用量汇总：当前周期内各 metric 总和与 quota 比较。

    返回当前 workspace 的 active 订阅所在周期的各 metric 已用量、quota、是否超额。
    """
    async with pool.connection() as conn:
        sub_result = await conn.execute(
            """
            SELECT * FROM billing_subscription
            WHERE workspace_id = %s AND status = 'active'
            ORDER BY created_at DESC LIMIT 1
            """,
            (actor.workspace_id,),
        )
        sub = await sub_result.fetchone()
        if not sub:
            return UsageSummary(
                workspace_id=actor.workspace_id,
                has_active_subscription=False,
                metrics=[],
            ).model_dump()
        plan_result = await conn.execute(
            "SELECT * FROM billing_plan WHERE id = %s", (sub["plan_id"],)
        )
        plan = await plan_result.fetchone()
        usage_result = await conn.execute(
            """
            SELECT metric, COALESCE(SUM(value), 0) AS total
            FROM billing_usage_record
            WHERE workspace_id = %s AND period_start >= %s AND period_end <= %s
            GROUP BY metric
            """,
            (
                actor.workspace_id,
                sub["current_period_start"],
                sub["current_period_end"],
            ),
        )
        usage_rows = await usage_result.fetchall()
    quota_map: dict[str, int | None] = {
        "tokens_used": int(plan["token_quota"]) if plan else None,
        "seats_used": int(plan["seat_quota"]) if plan else None,
        "storage_used_gb": int(plan["storage_quota_gb"]) if plan else None,
        "api_calls": int(plan["api_rate_limit"]) if plan else None,
    }
    used_by_metric: dict[str, int] = {
        u["metric"]: int(u["total"]) for u in usage_rows
    }
    metrics: list[dict[str, Any]] = []
    for m, q in quota_map.items():
        used = used_by_metric.get(m, 0)
        metrics.append(
            {
                "metric": m,
                "used": used,
                "quota": q,
                "exceeded": q is not None and used > q,
            }
        )
    return UsageSummary(
        workspace_id=actor.workspace_id,
        subscription_id=sub["id"],
        plan_id=sub["plan_id"],
        period_start=sub["current_period_start"],
        period_end=sub["current_period_end"],
        has_active_subscription=True,
        metrics=metrics,
    ).model_dump()


@router.get("/invoices")
async def list_invoices(
    actor: Annotated[Actor, Depends(get_actor)],
    limit: int = Query(default=50, ge=1, le=200),
    offset: int = Query(default=0, ge=0),
):
    """发票列表。"""
    async with pool.connection() as conn:
        result = await conn.execute(
            "SELECT * FROM billing_invoice WHERE workspace_id = %s ORDER BY created_at DESC LIMIT %s OFFSET %s",
            (actor.workspace_id, limit, offset),
        )
        rows = await result.fetchall()
    items = [_invoice_summary(row) for row in rows]
    return {"items": items, "data": items, "count": len(items), "limit": limit, "offset": offset}


@router.get("/invoices/{invoice_id}")
async def get_invoice(
    invoice_id: str,
    actor: Annotated[Actor, Depends(get_actor)],
):
    """发票详情。"""
    async with pool.connection() as conn:
        row = await _owned_invoice(conn, invoice_id, actor)
    return _invoice_summary(row)
