from datetime import date, datetime, timedelta, timezone
from typing import Annotated, Any

try:  # pragma: no cover
    from datetime import UTC
except ImportError:  # Python < 3.11 compatibility for the current runtime
    UTC = timezone.utc

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel, Field

from workama_platform.core import Actor, get_actor, json_dumps, new_id, pool, require_internal
from workama_platform.modules.billing.grants import expire_credit_grants_in_transaction
from workama_platform.modules.billing.metering import MeteringEvent, settle_meter_event
from workama_platform.modules.billing.reporting import run_daily_reconciliation

router = APIRouter(prefix="/api/v1/billing", tags=["billing"])
internal_router = APIRouter(prefix="/internal/billing", tags=["billing-internal"])
admin_router = APIRouter(prefix="/api/v1/admin/billing", tags=["billing-admin"])


class ReconciliationRunRequest(BaseModel):
    business_date: date
    workspace_id: str | None = Field(default=None, min_length=3, max_length=80)


class UsageExportRequest(BaseModel):
    format: str = Field(default="jsonl", pattern=r"^(jsonl|csv)$")
    start_date: date | None = None
    end_date: date | None = None


class ForecastQuery(BaseModel):
    days: int = Field(default=30, ge=1, le=90)


def _require_admin(actor: Actor) -> None:
    if actor.role not in {"owner", "admin"}:
        raise HTTPException(status_code=403, detail="Admin role required")


@router.get("/overview")
async def overview(actor: Annotated[Actor, Depends(get_actor)]):
    """Billing console overview for the current workspace."""
    month_start = datetime.now(UTC).replace(day=1, hour=0, minute=0, second=0, microsecond=0)
    async with pool.connection() as conn:
        plan_rows = await (await conn.execute(
            """
            SELECT code AS id, name, monthly_price AS price, currency, quotas
            FROM bill_plan
            WHERE active = TRUE
            ORDER BY monthly_price ASC, code ASC
            """
        )).fetchall()
        sub_row = await (await conn.execute(
            """
            SELECT s.id, s.plan_code, p.name AS plan_name, s.status,
                   s.current_period_start AS started_at, s.current_period_end AS renew_at,
                   p.quotas
            FROM bill_subscription s
            JOIN bill_plan p ON p.code = s.plan_code
            WHERE s.workspace_id = %s
            LIMIT 1
            """,
            (actor.workspace_id,),
        )).fetchone()
        usage_row = await (await conn.execute(
            """
            SELECT COUNT(*) AS requests,
                   COALESCE(SUM(prompt_tokens), 0) AS prompt_tokens,
                   COALESCE(SUM(completion_tokens), 0) AS completion_tokens,
                   COALESCE(SUM(cost_credits), 0) AS credits_used
            FROM bill_usage_record
            WHERE workspace_id = %s AND created_at >= %s
            """,
            (actor.workspace_id, month_start),
        )).fetchone()
        event_rows = await (await conn.execute(
            """
            SELECT id, kind AS type, amount, description, created_at
            FROM bill_transaction
            WHERE workspace_id = %s
            ORDER BY created_at DESC
            LIMIT 8
            """,
            (actor.workspace_id,),
        )).fetchall()

    plans = []
    for row in plan_rows:
        quotas = dict(row.get("quotas") or {})
        plans.append({
            "id": row["id"],
            "name": row["name"],
            "price": float(row["price"] or 0),
            "currency": row["currency"],
            "seats": quotas.get("members"),
            "monthly_credits": quotas.get("granted_credits_month"),
            "features": [
                f"{quotas.get('agent_concurrency') or 0} agents",
                f"{quotas.get('gateway_tokens') or 0} gateway tokens",
            ],
        })

    subscription = None
    if sub_row:
        quotas = dict(sub_row.get("quotas") or {})
        subscription = {
            "id": sub_row["id"],
            "plan_id": sub_row["plan_code"],
            "plan_code": sub_row["plan_code"],
            "plan_name": sub_row["plan_name"],
            "status": sub_row["status"],
            "seats": quotas.get("members"),
            "started_at": sub_row["started_at"],
            "renew_at": sub_row["renew_at"],
        }

    prompt_tokens = int(usage_row.get("prompt_tokens") or 0) if usage_row else 0
    completion_tokens = int(usage_row.get("completion_tokens") or 0) if usage_row else 0
    credits_used = float(usage_row.get("credits_used") or 0) if usage_row else 0.0
    usage = {
        "requests": int(usage_row.get("requests") or 0) if usage_row else 0,
        "tokens": prompt_tokens + completion_tokens,
        "storage_mb": 0,
        "credits_used": credits_used,
        "month": month_start.date().isoformat(),
    }
    events = []
    for row in event_rows:
        events.append({
            "id": row["id"],
            "type": row["type"],
            "amount": float(row["amount"] or 0),
            "currency": "credits",
            "description": row.get("description") or row["type"],
            "created_at": row["created_at"],
        })
    return {"plans": plans, "subscription": subscription, "usage": usage, "events": events}

@router.get("/account")
async def account(actor: Annotated[Actor, Depends(get_actor)]):
    async with pool.connection() as conn:
        async with conn.transaction():
            await expire_credit_grants_in_transaction(conn, actor.workspace_id)
            result = await conn.execute(
                """
                SELECT id, granted_balance, purchased_balance, frozen_balance,
                       granted_balance + purchased_balance AS total_balance,
                       granted_balance + purchased_balance - frozen_balance AS available_balance,
                       version, updated_at
                FROM bill_account WHERE workspace_id = %s
                """,
                (actor.workspace_id,),
            )
            return await result.fetchone()


@router.get("/grants")
async def grants(actor: Annotated[Actor, Depends(get_actor)]):
    async with pool.connection() as conn:
        async with conn.transaction():
            await expire_credit_grants_in_transaction(conn, actor.workspace_id)
            result = await conn.execute(
                """
                SELECT id,source,subscription_id,period_start,expires_at,
                       initial_amount,remaining_amount,status,created_at,expired_at
                FROM bill_credit_grant WHERE workspace_id=%s
                ORDER BY expires_at NULLS LAST, period_start DESC, created_at DESC
                """,
                (actor.workspace_id,),
            )
            rows = await result.fetchall()
            return {"items": rows, "data": rows, "next_cursor": None, "has_more": False, "meta": {"request_id": None}}

@router.get("/transactions")
async def transactions(
    actor: Annotated[Actor, Depends(get_actor)], limit: int = 50
):
    async with pool.connection() as conn:
        result = await conn.execute(
            """
            SELECT id, kind, amount, balance_after, reference_id, description, created_at
            FROM bill_transaction WHERE workspace_id = %s
            ORDER BY created_at DESC LIMIT %s
            """,
            (actor.workspace_id, min(max(limit, 1), 100)),
        )
        rows = await result.fetchall()
        return {"items": rows, "data": rows, "next_cursor": None, "has_more": False, "meta": {"request_id": None}}

@router.get("/usage")
async def usage(actor: Annotated[Actor, Depends(get_actor)]):
    async with pool.connection() as conn:
        result = await conn.execute(
            """
            SELECT model, COUNT(*) AS requests,
                   SUM(prompt_tokens) AS prompt_tokens,
                   SUM(completion_tokens) AS completion_tokens,
                   SUM(cost_credits) AS cost_credits
            FROM bill_usage_record WHERE workspace_id = %s GROUP BY model ORDER BY model
            """,
            (actor.workspace_id,),
        )
        hourly_result = await conn.execute(
            """
            SELECT resource, model, hour, requests, prompt_tokens,
                   completion_tokens, cost_credits
            FROM bill_usage_hourly WHERE workspace_id = %s
            ORDER BY hour DESC, model LIMIT 168
            """,
            (actor.workspace_id,),
        )
        rows = await result.fetchall()
        hourly_rows = await hourly_result.fetchall()
        return {"items": rows, "data": rows, "next_cursor": None, "has_more": False, "meta": {"request_id": None}, "hourly": hourly_rows}
@router.get("/reconciliations")
async def reconciliations(actor: Annotated[Actor, Depends(get_actor)], limit: int = 31):
    async with pool.connection() as conn:
        result = await conn.execute(
            """
            SELECT id, business_date, usage_credits, ledger_credits,
                   difference, difference_ratio, status, checked_at
            FROM bill_reconciliation_run WHERE workspace_id = %s
            ORDER BY business_date DESC LIMIT %s
            """,
            (actor.workspace_id, min(max(limit, 1), 366)),
        )
        rows = await result.fetchall()
        return {"items": rows, "data": rows, "next_cursor": None, "has_more": False, "meta": {"request_id": None}}

@internal_router.post("/reconcile", dependencies=[Depends(require_internal)])
async def reconcile(business_date: date, workspace_id: str | None = None):
    items = await run_daily_reconciliation(business_date, workspace_id)
    return {"items": items, "data": items, "next_cursor": None, "has_more": False, "meta": {"request_id": None}}

@internal_router.post("/meter-events", dependencies=[Depends(require_internal)])
async def record_meter_event(body: MeteringEvent) -> dict[str, Any]:
    """Receive an async metering event and settle it idempotently.

    This endpoint is the billing-module counterpart to the gateway's NATS
    JetStream publisher: the same envelope consumed by platform-worker can be
    accepted directly over HTTP for testing, repair, or fallback scenarios.
    """
    processed = await settle_meter_event(body, body.event_type, "billing-metering-v1")
    return {"event_id": body.event_id, "status": "processed" if processed else "duplicate"}


@internal_router.get("/meter-events/{event_id}", dependencies=[Depends(require_internal)])
async def get_meter_event(event_id: str) -> dict[str, Any]:
    async with pool.connection() as conn:
        result = await conn.execute(
            """
            SELECT event_id, subject, consumer_name, status, request_id,
                   received_at, processed_at, last_error
            FROM ops_inbox WHERE event_id = %s
            """,
            (event_id,),
        )
        row = await result.fetchone()
    if not row:
        raise HTTPException(status_code=404, detail="Meter event not found")
    return dict(row)


@admin_router.get("/reconciliations")
async def admin_reconciliations(actor: Annotated[Actor, Depends(get_actor)], limit: int = 31):
    _require_admin(actor)
    async with pool.connection() as conn:
        result = await conn.execute(
            """
            SELECT id,workspace_id,business_date,usage_credits,ledger_credits,
                   difference,difference_ratio,status,checked_at
            FROM bill_reconciliation_run WHERE workspace_id=%s
            ORDER BY business_date DESC LIMIT %s
            """,
            (actor.workspace_id, min(max(limit, 1), 366)),
        )
        rows = await result.fetchall()
        return {"items": rows, "data": rows, "next_cursor": None, "has_more": False, "meta": {"request_id": None}}

@admin_router.get("/reconciliations/{reconciliation_id}")
async def get_admin_reconciliation(reconciliation_id: str, actor: Annotated[Actor, Depends(get_actor)]):
    _require_admin(actor)
    async with pool.connection() as conn:
        result = await conn.execute(
            "SELECT id,workspace_id,business_date,usage_credits,ledger_credits,difference,difference_ratio,status,checked_at FROM bill_reconciliation_run WHERE id=%s AND workspace_id=%s",
            (reconciliation_id, actor.workspace_id),
        )
        row = await result.fetchone()
    if not row:
        raise HTTPException(status_code=404, detail="Reconciliation not found")
    return row


@admin_router.post("/reconciliations", status_code=status.HTTP_202_ACCEPTED)
async def run_admin_reconciliation(body: ReconciliationRunRequest, actor: Annotated[Actor, Depends(get_actor)]):
    _require_admin(actor)
    workspace_id = body.workspace_id or actor.workspace_id
    if workspace_id != actor.workspace_id:
        raise HTTPException(status_code=403, detail="Cross-workspace reconciliation is not allowed")
    items = await run_daily_reconciliation(body.business_date, workspace_id)
    return {"status": "completed", "items": items, "data": items, "next_cursor": None, "has_more": False, "meta": {"request_id": None}, "verification_scope": "local-compose"}


@router.get("/transactions/{transaction_id}")
async def get_billing_transaction(transaction_id: str, actor: Annotated[Actor, Depends(get_actor)]) -> dict[str, Any]:
    async with pool.connection() as conn:
        result = await conn.execute(
            """
            SELECT id, kind, amount, balance_after, reference_id, description, created_at
            FROM bill_transaction WHERE id=%s AND workspace_id=%s
            """,
            (transaction_id, actor.workspace_id),
        )
        row = await result.fetchone()
    if not row:
        raise HTTPException(status_code=404, detail="Transaction not found")
    return dict(row)


@router.post("/usage-exports", status_code=status.HTTP_202_ACCEPTED)
async def create_billing_usage_export(
    body: UsageExportRequest,
    actor: Annotated[Actor, Depends(get_actor)],
) -> dict[str, Any]:
    """Queue a usage export. When no real provider is configured the export is
    completed locally by a controlled mock runner.
    """
    export_id = new_id("buex")
    filter_json = body.model_dump(exclude_none=True)
    manifest = {
        "schema_version": "workama.billing-usage-export.v1",
        "format": body.format,
        "filter": filter_json,
        "provider_execution": "pending_external",
    }
    async with pool.connection() as conn:
        result = await conn.execute(
            """
            INSERT INTO bill_usage_export(
                id, org_id, workspace_id, status, format, filter_json,
                record_count, content_hash, manifest, created_by, expires_at
            ) VALUES (%s, %s, %s, 'queued', %s, %s::jsonb, 0, %s, %s::jsonb, %s, %s)
            RETURNING id, status, format, record_count, content_hash, manifest, created_at, expires_at
            """,
            (
                export_id,
                actor.org_id,
                actor.workspace_id,
                body.format,
                json_dumps(filter_json),
                "",
                json_dumps(manifest),
                actor.user_id,
                datetime.now(UTC) + timedelta(hours=24),
            ),
        )
        row = await result.fetchone()
        await conn.commit()
    return {
        **dict(row),
        "operation_id": export_id,
        "status": "accepted",
        "execution_mode": "controlled_mock",
    }


@router.get("/forecast")
async def get_billing_forecast(
    actor: Annotated[Actor, Depends(get_actor)],
    days: int = 30,
) -> dict[str, Any]:
    """Return a workspace usage forecast. Falls back to deterministic mock
    values when the workspace has no recent usage history.
    """
    query_days = min(max(days, 1), 90)
    end_at = datetime.now(UTC)
    start_at = end_at - timedelta(days=query_days)
    async with pool.connection() as conn:
        result = await conn.execute(
            """
            SELECT COALESCE(SUM(cost_credits), 0) AS total_credits,
                   COALESCE(SUM(prompt_tokens) + SUM(completion_tokens), 0) AS total_tokens,
                   COALESCE(COUNT(*), 0) AS requests
            FROM bill_usage_record
            WHERE workspace_id=%s AND created_at >= %s AND created_at <= %s
            """,
            (actor.workspace_id, start_at, end_at),
        )
        history = await result.fetchone()
        account_result = await conn.execute(
            "SELECT granted_balance + purchased_balance - frozen_balance AS available_balance FROM bill_account WHERE workspace_id=%s",
            (actor.workspace_id,),
        )
        account = await account_result.fetchone()

    history_credits = float(history["total_credits"] or 0)
    history_tokens = int(history["total_tokens"] or 0)
    history_requests = int(history["requests"] or 0)

    if history_credits > 0:
        daily_avg = history_credits / query_days
        projected = round(daily_avg * 30, 6)
    else:
        # Deterministic mock fallback for workspaces without real usage history.
        daily_avg = round(12.5 + (hash(actor.workspace_id) % 1000) / 100, 6)
        projected = round(daily_avg * 30, 6)

    available_balance = float(account["available_balance"] if account else 0)
    return {
        "workspace_id": actor.workspace_id,
        "forecast_days": query_days,
        "daily_average_credits": daily_avg,
        "projected_monthly_credits": projected,
        "recent_credits": history_credits,
        "recent_tokens": history_tokens,
        "recent_requests": history_requests,
        "available_balance": available_balance,
        "balance_coverage_days": round(available_balance / daily_avg, 2) if daily_avg > 0 else None,
        "provider_execution": "local",
    }
