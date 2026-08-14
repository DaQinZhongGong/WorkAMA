from __future__ import annotations

import hashlib
import hmac
import json
from datetime import UTC, datetime, timedelta
from decimal import Decimal, InvalidOperation
from typing import Annotated, Any, Literal

from fastapi import APIRouter, Depends, Header, HTTPException, Request, status
from pydantic import BaseModel, Field, model_validator

from workama_platform.core import Actor, get_actor, hash_secret, json_dumps, new_id, pool, settings
from workama_platform.modules.audit_exports import append_audit_chain
from workama_platform.modules.billing.grants import grant_credits_in_transaction


router = APIRouter(prefix="/api/v1/billing", tags=["subscriptions"])

PLAN_CATALOG: dict[str, dict[str, Any]] = {
    "free": {
        "name": "Free", "monthly_price": 0, "currency": "CNY",
        "quotas": {"granted_credits_month": 500, "members": 1, "workspaces": 1, "agent_concurrency": 1, "max_steps": 20, "max_credits": 100, "max_duration_seconds": 1800, "dataset_bytes": 100 * 1024 * 1024, "gateway_tokens": 5, "published_apps": 1},
    },
    "pro": {
        "name": "Pro", "monthly_price": 99, "currency": "CNY",
        "quotas": {"granted_credits_month": 12000, "members": 1, "workspaces": 3, "agent_concurrency": 3, "max_steps": 50, "max_credits": 500, "max_duration_seconds": 3600, "dataset_bytes": 5 * 1024 * 1024 * 1024, "gateway_tokens": 50, "published_apps": 10},
    },
    "team": {
        "name": "Team", "monthly_price": 499, "currency": "CNY",
        "quotas": {"granted_credits_month": 60000, "members": 10, "workspaces": 10, "agent_concurrency": 10, "max_steps": 50, "max_credits": 500, "max_duration_seconds": 3600, "dataset_bytes": 50 * 1024 * 1024 * 1024, "gateway_tokens": 500, "published_apps": 100},
    },
    "enterprise": {
        "name": "Enterprise", "monthly_price": 0, "currency": "CNY",
        "quotas": {"granted_credits_month": None, "members": None, "workspaces": None, "agent_concurrency": None, "max_steps": None, "max_credits": None, "max_duration_seconds": None, "dataset_bytes": None, "gateway_tokens": None, "published_apps": None},
    },
}


class CheckoutRequest(BaseModel):
    plan_code: str = Field(min_length=1, max_length=32)
    provider: Literal["mock", "wechat", "alipay", "stripe"] = "mock"
    idempotency_key: str = Field(min_length=8, max_length=128)


class ConfirmPaymentRequest(BaseModel):
    provider_event_id: str = Field(min_length=8, max_length=160)
    outcome: Literal["succeeded", "failed"] = "succeeded"


class OrderCreateRequest(BaseModel):
    order_type: Literal["subscription", "credits"] = "subscription"
    plan_code: str | None = Field(default=None, min_length=1, max_length=32)
    amount: Decimal | None = Field(default=None, gt=0, max_digits=18, decimal_places=2)
    credits: Decimal | None = Field(default=None, gt=0, max_digits=18, decimal_places=6)
    provider: Literal["mock", "wechat", "alipay", "stripe"] = "mock"
    region: str = Field(default="CN", min_length=2, max_length=16)
    tax_mode: Literal["exclusive", "inclusive"] = "exclusive"
    idempotency_key: str = Field(min_length=8, max_length=128)

    @model_validator(mode="after")
    def validate_order(self):
        if self.order_type == "subscription" and not self.plan_code:
            raise ValueError("plan_code is required for subscription orders")
        if self.order_type == "credits" and self.amount is None and self.credits is None:
            raise ValueError("amount or credits is required for credit orders")
        if self.order_type == "subscription" and (self.amount is not None or self.credits is not None):
            raise ValueError("subscription orders use the selected plan price")
        return self


class OrderCancelRequest(BaseModel):
    reason: str = Field(min_length=3, max_length=500)
    idempotency_key: str = Field(min_length=8, max_length=128)


class PaymentMethodSetupRequest(BaseModel):
    provider: Literal["mock", "wechat", "alipay", "stripe"] = "mock"
    method_type: Literal["card", "wallet", "bank_transfer"] = "card"
    token: str = Field(min_length=8, max_length=4096)
    display_label: str = Field(default="Payment method", min_length=2, max_length=120)


class DeletePaymentMethodRequest(BaseModel):
    reason: str = Field(min_length=3, max_length=500)


class SubscriptionChangeRequest(BaseModel):
    plan_code: str = Field(min_length=1, max_length=32)


class SubscriptionChangeConfirm(BaseModel):
    plan_code: str = Field(min_length=1, max_length=32)
    idempotency_key: str = Field(min_length=8, max_length=128)
    provider: Literal["mock", "wechat", "alipay", "stripe"] = "mock"


class InvoiceRequestCreate(BaseModel):
    order_id: str = Field(min_length=3, max_length=80)
    tax_profile: dict[str, Any] = Field(default_factory=dict)
    idempotency_key: str = Field(min_length=8, max_length=128)


class RefundCreateRequest(BaseModel):
    payment_id: str | None = Field(default=None, min_length=3, max_length=80)
    order_id: str | None = Field(default=None, min_length=3, max_length=80)
    amount: Decimal | None = Field(default=None, gt=0, max_digits=18, decimal_places=2)
    reason: str = Field(min_length=3, max_length=500)
    idempotency_key: str = Field(min_length=8, max_length=128)
    approval_ref: str | None = Field(default=None, max_length=160)

    @model_validator(mode="after")
    def validate_target(self):
        if not self.payment_id and not self.order_id:
            raise ValueError("payment_id or order_id is required")
        return self


def ensure_subscription_schema_statements() -> tuple[str, ...]:
    return (
        """
        CREATE TABLE IF NOT EXISTS bill_plan (
          code TEXT PRIMARY KEY,
          name TEXT NOT NULL,
          monthly_price NUMERIC(18,2) NOT NULL DEFAULT 0,
          currency TEXT NOT NULL DEFAULT 'CNY',
          quotas JSONB NOT NULL DEFAULT '{}'::jsonb,
          active BOOLEAN NOT NULL DEFAULT TRUE,
          created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
          updated_at TIMESTAMPTZ NOT NULL DEFAULT now()
        )
        """,
        """
        CREATE TABLE IF NOT EXISTS bill_subscription (
          id TEXT PRIMARY KEY,
          org_id TEXT NOT NULL REFERENCES id_org(id) ON DELETE CASCADE,
          workspace_id TEXT NOT NULL UNIQUE REFERENCES id_workspace(id) ON DELETE CASCADE,
          plan_code TEXT NOT NULL REFERENCES bill_plan(code),
          status TEXT NOT NULL DEFAULT 'active' CHECK (status IN ('active','past_due','canceled')),
          current_period_start TIMESTAMPTZ NOT NULL,
          current_period_end TIMESTAMPTZ NOT NULL,
          cancel_at_period_end BOOLEAN NOT NULL DEFAULT FALSE,
          pending_plan_code TEXT REFERENCES bill_plan(code),
          provider_customer_ref TEXT,
          created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
          updated_at TIMESTAMPTZ NOT NULL DEFAULT now()
        )
        """,
        """
        CREATE TABLE IF NOT EXISTS bill_payment (
          id TEXT PRIMARY KEY,
          workspace_id TEXT NOT NULL REFERENCES id_workspace(id) ON DELETE CASCADE,
          subscription_id TEXT REFERENCES bill_subscription(id) ON DELETE SET NULL,
          provider TEXT NOT NULL,
          idempotency_key TEXT NOT NULL,
          provider_event_id TEXT UNIQUE,
          plan_code TEXT NOT NULL REFERENCES bill_plan(code),
          amount NUMERIC(18,2) NOT NULL,
          currency TEXT NOT NULL,
          status TEXT NOT NULL CHECK (status IN ('pending','succeeded','failed','unknown','charged_back','refunded')),
          metadata JSONB NOT NULL DEFAULT '{}'::jsonb,
          created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
          updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
          UNIQUE(workspace_id, idempotency_key)
        )
        """,
        """
        CREATE TABLE IF NOT EXISTS bill_invoice (
          id TEXT PRIMARY KEY,
          workspace_id TEXT NOT NULL REFERENCES id_workspace(id) ON DELETE CASCADE,
          payment_id TEXT NOT NULL UNIQUE REFERENCES bill_payment(id),
          invoice_number TEXT NOT NULL UNIQUE,
          status TEXT NOT NULL DEFAULT 'issued' CHECK (status IN ('issued','void','credited')),
          amount NUMERIC(18,2) NOT NULL,
          currency TEXT NOT NULL,
          tax_profile JSONB NOT NULL DEFAULT '{}'::jsonb,
          issued_at TIMESTAMPTZ NOT NULL DEFAULT now()
        )
        """,
        "ALTER TABLE bill_payment ADD COLUMN IF NOT EXISTS order_id TEXT",
        "ALTER TABLE bill_invoice ADD COLUMN IF NOT EXISTS order_id TEXT",
        """
        CREATE TABLE IF NOT EXISTS bill_price_catalog (
          id TEXT PRIMARY KEY,
          plan_code TEXT NOT NULL REFERENCES bill_plan(code),
          region TEXT NOT NULL,
          currency TEXT NOT NULL,
          tax_mode TEXT NOT NULL CHECK (tax_mode IN ('exclusive','inclusive')),
          price NUMERIC(18,2) NOT NULL CHECK (price >= 0),
          tax_rate NUMERIC(8,6) NOT NULL DEFAULT 0 CHECK (tax_rate >= 0),
          version INTEGER NOT NULL,
          effective_from TIMESTAMPTZ NOT NULL DEFAULT now(),
          effective_to TIMESTAMPTZ,
          active BOOLEAN NOT NULL DEFAULT TRUE,
          created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
          UNIQUE(plan_code, region, currency, tax_mode, version)
        )
        """,
        """
        CREATE TABLE IF NOT EXISTS bill_order (
          id TEXT PRIMARY KEY,
          org_id TEXT NOT NULL REFERENCES id_org(id) ON DELETE CASCADE,
          workspace_id TEXT NOT NULL REFERENCES id_workspace(id) ON DELETE CASCADE,
          order_no TEXT NOT NULL UNIQUE,
          order_type TEXT NOT NULL CHECK (order_type IN ('subscription','credits')),
          plan_code TEXT REFERENCES bill_plan(code),
          amount NUMERIC(18,2) NOT NULL CHECK (amount >= 0),
          currency TEXT NOT NULL,
          credits NUMERIC(18,6) NOT NULL DEFAULT 0 CHECK (credits >= 0),
          price_snapshot JSONB NOT NULL DEFAULT '{}'::jsonb,
          tax_snapshot JSONB NOT NULL DEFAULT '{}'::jsonb,
          discount_snapshot JSONB NOT NULL DEFAULT '{}'::jsonb,
          status TEXT NOT NULL DEFAULT 'pending' CHECK (status IN ('pending','succeeded','failed','canceled','unknown','charged_back','partially_refunded','refunded')),
          expires_at TIMESTAMPTZ NOT NULL DEFAULT (now() + interval '30 minutes'),
          idempotency_key TEXT NOT NULL,
          version BIGINT NOT NULL DEFAULT 1,
          created_by TEXT NOT NULL REFERENCES id_user(id),
          created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
          updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
          UNIQUE(workspace_id, idempotency_key)
        )
        """,
        """
        CREATE TABLE IF NOT EXISTS bill_payment_event (
          id TEXT PRIMARY KEY,
          provider TEXT NOT NULL,
          provider_event_id TEXT NOT NULL,
          payment_id TEXT NOT NULL REFERENCES bill_payment(id) ON DELETE CASCADE,
          payload_hash TEXT NOT NULL,
          outcome TEXT NOT NULL,
          received_at TIMESTAMPTZ NOT NULL DEFAULT now(),
          applied_at TIMESTAMPTZ,
          UNIQUE(provider, provider_event_id)
        )
        """,
        """
        CREATE TABLE IF NOT EXISTS bill_payment_method (
          id TEXT PRIMARY KEY,
          workspace_id TEXT NOT NULL REFERENCES id_workspace(id) ON DELETE CASCADE,
          provider TEXT NOT NULL,
          method_type TEXT NOT NULL,
          display_label TEXT NOT NULL,
          provider_ref_hash TEXT NOT NULL,
          status TEXT NOT NULL DEFAULT 'active' CHECK (status IN ('active','detached')),
          created_by TEXT NOT NULL REFERENCES id_user(id),
          created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
          detached_at TIMESTAMPTZ,
          detach_reason TEXT,
          UNIQUE(workspace_id, provider, provider_ref_hash)
        )
        """,
        """
        CREATE TABLE IF NOT EXISTS bill_refund (
          id TEXT PRIMARY KEY,
          workspace_id TEXT NOT NULL REFERENCES id_workspace(id) ON DELETE CASCADE,
          payment_id TEXT NOT NULL REFERENCES bill_payment(id),
          order_id TEXT REFERENCES bill_order(id),
          amount NUMERIC(18,2) NOT NULL CHECK (amount > 0),
          currency TEXT NOT NULL,
          reason TEXT NOT NULL,
          status TEXT NOT NULL DEFAULT 'pending' CHECK (status IN ('pending','succeeded','failed','unknown','pending_external')),
          provider_ref_hash TEXT,
          idempotency_key TEXT NOT NULL,
          approval_ref TEXT,
          created_by TEXT NOT NULL REFERENCES id_user(id),
          created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
          updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
          UNIQUE(workspace_id, idempotency_key)
        )
        """,
        """
        CREATE TABLE IF NOT EXISTS bill_invoice_request (
          id TEXT PRIMARY KEY,
          workspace_id TEXT NOT NULL REFERENCES id_workspace(id) ON DELETE CASCADE,
          order_id TEXT NOT NULL REFERENCES bill_order(id),
          tax_profile JSONB NOT NULL DEFAULT '{}'::jsonb,
          status TEXT NOT NULL DEFAULT 'pending_external' CHECK (status IN ('pending_external','queued','issued','failed')),
          idempotency_key TEXT NOT NULL,
          created_by TEXT NOT NULL REFERENCES id_user(id),
          created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
          updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
          UNIQUE(workspace_id, idempotency_key)
        )
        """,
        "CREATE INDEX IF NOT EXISTS idx_bill_payment_workspace_time ON bill_payment(workspace_id, created_at DESC)",
        "CREATE INDEX IF NOT EXISTS idx_bill_invoice_workspace_time ON bill_invoice(workspace_id, issued_at DESC)",
        "CREATE INDEX IF NOT EXISTS idx_bill_order_workspace_time ON bill_order(workspace_id, created_at DESC)",
        "CREATE INDEX IF NOT EXISTS idx_bill_payment_event_payment ON bill_payment_event(payment_id, received_at DESC)",
        "CREATE INDEX IF NOT EXISTS idx_bill_refund_workspace_time ON bill_refund(workspace_id, created_at DESC)",
    )


async def ensure_subscription_schema(conn) -> None:
    for statement in ensure_subscription_schema_statements():
        await conn.execute(statement)
    for code, plan in PLAN_CATALOG.items():
        await conn.execute(
            """
            INSERT INTO bill_plan(code,name,monthly_price,currency,quotas)
            VALUES (%s,%s,%s,%s,%s::jsonb)
            ON CONFLICT(code) DO UPDATE SET name=EXCLUDED.name, monthly_price=EXCLUDED.monthly_price,
              currency=EXCLUDED.currency, quotas=EXCLUDED.quotas, updated_at=now()
            """,
            (code, plan["name"], plan["monthly_price"], plan["currency"], json_dumps(plan["quotas"])),
        )
        await conn.execute(
            """
            INSERT INTO bill_price_catalog(
              id,plan_code,region,currency,tax_mode,price,tax_rate,version
            ) VALUES (%s,%s,'CN',%s,'exclusive',%s,0,1)
            ON CONFLICT(plan_code,region,currency,tax_mode,version) DO UPDATE
              SET price=EXCLUDED.price, active=TRUE
            """,
            (f"price_{code}_v1", code, plan["currency"], plan["monthly_price"]),
        )
    await conn.execute(
        """
        INSERT INTO bill_subscription(id,org_id,workspace_id,plan_code,current_period_start,current_period_end)
        SELECT 'sub_' || w.id, w.org_id, w.id, 'free', now(), now() + interval '30 days'
        FROM id_workspace w LEFT JOIN bill_subscription s ON s.workspace_id=w.id
        WHERE s.id IS NULL
        """
    )


def _owner(actor: Actor) -> None:
    if actor.role != "owner":
        raise HTTPException(status_code=403, detail="Organization owner required")


def _plan(code: str) -> dict[str, Any]:
    if code not in PLAN_CATALOG:
        raise HTTPException(status_code=422, detail=f"Unknown subscription plan: {code}")
    return PLAN_CATALOG[code]


def _require_step_up(actor: Actor, operation: str) -> None:
    if actor.auth_strength < 2:
        raise HTTPException(
            status_code=403,
            detail={
                "code": "E00009",
                "message": f"{operation} requires step-up authentication",
                "required_auth_strength": 2,
                "actual_auth_strength": actor.auth_strength,
            },
        )


def _order_summary(row: dict[str, Any]) -> dict[str, Any]:
    return {
        "id": row["id"],
        "order_no": row["order_no"],
        "workspace_id": row["workspace_id"],
        "order_type": row["order_type"],
        "plan_code": row.get("plan_code"),
        "amount": row["amount"],
        "currency": row["currency"],
        "credits": row["credits"],
        "price_snapshot": row["price_snapshot"],
        "tax_snapshot": row["tax_snapshot"],
        "discount_snapshot": row["discount_snapshot"],
        "status": row["status"],
        "expires_at": row["expires_at"],
        "version": row["version"],
        "created_at": row["created_at"],
        "updated_at": row["updated_at"],
    }


async def _order_with_payment(conn, workspace_id: str, order_id: str) -> dict[str, Any] | None:
    result = await conn.execute(
        """
        SELECT o.*, p.id AS payment_id, p.provider, p.provider_event_id,
               p.status AS payment_status, p.amount AS payment_amount,
               p.currency AS payment_currency
        FROM bill_order o LEFT JOIN bill_payment p ON p.order_id=o.id
        WHERE o.workspace_id=%s AND o.id=%s
        """,
        (workspace_id, order_id),
    )
    return await result.fetchone()


async def _create_order_in_transaction(conn, actor: Actor, body: OrderCreateRequest) -> tuple[dict[str, Any], dict[str, Any], bool]:
    existing = await conn.execute(
        """
        SELECT o.*, p.id AS payment_id, p.provider, p.provider_event_id,
               p.status AS payment_status, p.amount AS payment_amount,
               p.currency AS payment_currency
        FROM bill_order o LEFT JOIN bill_payment p ON p.order_id=o.id
        WHERE o.workspace_id=%s AND o.idempotency_key=%s
        FOR UPDATE OF o
        """,
        (actor.workspace_id, body.idempotency_key),
    )
    existing_row = await existing.fetchone()
    if existing_row:
        payment = {
            "id": existing_row.get("payment_id"),
            "provider": existing_row.get("provider"),
            "provider_event_id": existing_row.get("provider_event_id"),
            "status": existing_row.get("payment_status"),
            "amount": existing_row.get("payment_amount"),
            "currency": existing_row.get("payment_currency"),
        }
        return _order_summary(existing_row), payment, True

    plan_code = body.plan_code if body.order_type == "subscription" else "free"
    plan = _plan(plan_code)
    subscription = await _ensure_subscription(conn, actor) if body.order_type == "subscription" else None
    if subscription and body.plan_code == subscription["plan_code"]:
        raise HTTPException(status_code=409, detail="Subscription already uses this plan")

    price_result = await conn.execute(
        """
        SELECT id, price, currency, tax_rate, version
        FROM bill_price_catalog
        WHERE plan_code=%s AND region=%s AND currency=%s AND tax_mode=%s
          AND active=TRUE AND effective_from<=now()
          AND (effective_to IS NULL OR effective_to>now())
        ORDER BY version DESC LIMIT 1
        """,
        (plan_code, body.region.upper(), plan["currency"], body.tax_mode),
    )
    price = await price_result.fetchone()
    if body.order_type == "subscription":
        amount = Decimal(str(price["price"] if price else plan["monthly_price"])).quantize(Decimal("0.01"))
        credits = Decimal("0")
        currency = str(price["currency"] if price else plan["currency"])
    else:
        amount = body.amount or (body.credits / Decimal("100"))
        amount = amount.quantize(Decimal("0.01"))
        credits = body.credits or (amount * Decimal("100")).quantize(Decimal("0.000001"))
        currency = "CNY"
        if amount > Decimal("100000") or credits > Decimal("10000000"):
            raise HTTPException(status_code=422, detail="Credit order exceeds local safety limit")
    order_id = new_id("ord")
    order = await conn.execute(
        """
        INSERT INTO bill_order(
          id,org_id,workspace_id,order_no,order_type,plan_code,amount,currency,credits,
          price_snapshot,tax_snapshot,discount_snapshot,idempotency_key,created_by
        ) VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s::jsonb,%s::jsonb,%s::jsonb,%s,%s)
        RETURNING *
        """,
        (
            order_id, actor.org_id, actor.workspace_id, f"WMA-{order_id[-12:]}",
            body.order_type, plan_code, amount, currency, credits,
            json_dumps({
                "catalog_id": price["id"] if price else f"plan_{plan_code}_fallback",
                "region": body.region.upper(), "currency": currency,
                "tax_mode": body.tax_mode, "version": price["version"] if price else 0,
                "unit_price": str(amount),
            }),
            json_dumps({"mode": body.tax_mode, "rate": str(price["tax_rate"] if price else Decimal("0"))}),
            json_dumps({}), body.idempotency_key, actor.user_id,
        ),
    )
    order_row = await order.fetchone()
    payment_id = new_id("pay")
    payment_result = await conn.execute(
        """
        INSERT INTO bill_payment(
          id,workspace_id,subscription_id,order_id,provider,idempotency_key,
          plan_code,amount,currency,status,metadata
        ) VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,'pending',%s::jsonb)
        RETURNING *
        """,
        (
            payment_id, actor.workspace_id, subscription["id"] if subscription else None,
            order_id, body.provider, body.idempotency_key, plan_code, amount, currency,
            json_dumps({"order_type": body.order_type, "credits": str(credits)}),
        ),
    )
    payment_row = await payment_result.fetchone()
    if subscription:
        await conn.execute(
            "UPDATE bill_subscription SET pending_plan_code=%s,updated_at=now() WHERE id=%s",
            (body.plan_code, subscription["id"]),
        )
    return _order_summary(order_row), payment_row, False


def _mock_signature(payload: bytes) -> str:
    return hmac.new(settings.billing_mock_webhook_secret.encode(), payload, hashlib.sha256).hexdigest()


async def _apply_payment_event(
    conn,
    *,
    provider: str,
    payment_id: str,
    provider_event_id: str,
    outcome: str,
    payload_hash: str,
    amount: Any = None,
    currency: str | None = None,
) -> dict[str, Any]:
    event_result = await conn.execute(
        "SELECT * FROM bill_payment_event WHERE provider=%s AND provider_event_id=%s FOR UPDATE",
        (provider, provider_event_id),
    )
    existing_event = await event_result.fetchone()
    if existing_event:
        if existing_event["payload_hash"] != payload_hash or existing_event["payment_id"] != payment_id:
            raise HTTPException(status_code=409, detail="Provider event id was already used with different data")
        payment_result = await conn.execute("SELECT * FROM bill_payment WHERE id=%s", (payment_id,))
        return {"payment": await payment_result.fetchone(), "replayed": True, "provider_event_id": provider_event_id}
    result = await conn.execute("SELECT * FROM bill_payment WHERE id=%s FOR UPDATE", (payment_id,))
    payment = await result.fetchone()
    if not payment:
        raise HTTPException(status_code=404, detail="Payment not found")
    if payment["provider"] != provider:
        raise HTTPException(status_code=409, detail="Provider does not match the payment")
    if amount is not None and Decimal(str(amount)).quantize(Decimal("0.01")) != Decimal(str(payment["amount"])).quantize(Decimal("0.01")):
        raise HTTPException(status_code=409, detail="Provider amount does not match the order snapshot")
    if currency and currency != payment["currency"]:
        raise HTTPException(status_code=409, detail="Provider currency does not match the order snapshot")
    if payment["status"] not in {"pending", "unknown"}:
        raise HTTPException(status_code=409, detail="Payment is already finalized")

    order = None
    if payment.get("order_id"):
        order_result = await conn.execute(
            "SELECT * FROM bill_order WHERE id=%s AND workspace_id=%s FOR UPDATE",
            (payment["order_id"], payment["workspace_id"]),
        )
        order = await order_result.fetchone()
    status_value = outcome
    if outcome == "succeeded":
        if order and order["order_type"] == "credits" and order["credits"]:
            account_result = await conn.execute(
                "SELECT id,granted_balance,purchased_balance,frozen_balance FROM bill_account WHERE workspace_id=%s FOR UPDATE",
                (payment["workspace_id"],),
            )
            account = await account_result.fetchone()
            if not account:
                raise HTTPException(status_code=503, detail="Billing account is not ready")
            balance_after = account["granted_balance"] + account["purchased_balance"] + order["credits"]
            await conn.execute(
                "UPDATE bill_account SET purchased_balance=purchased_balance+%s,version=version+1,updated_at=now() WHERE id=%s",
                (order["credits"], account["id"]),
            )
            await conn.execute(
                """
                INSERT INTO bill_transaction(id,workspace_id,kind,amount,balance_after,reference_id,description)
                VALUES (%s,%s,'purchase',%s,%s,%s,%s)
                ON CONFLICT(workspace_id,reference_id,kind) DO NOTHING
                """,
                (new_id("txn"), payment["workspace_id"], order["credits"], balance_after, order["id"], "Credit order purchase"),
            )
        if payment.get("subscription_id"):
            start = datetime.now(UTC)
            end = start + timedelta(days=30)
            await conn.execute(
                """
                UPDATE bill_subscription SET plan_code=%s,status='active',current_period_start=%s,
                  current_period_end=%s,pending_plan_code=NULL,cancel_at_period_end=FALSE,updated_at=now()
                WHERE id=%s
                """,
                (payment["plan_code"], start, end, payment["subscription_id"]),
            )
            monthly_grant = PLAN_CATALOG.get(payment["plan_code"], {}).get("quotas", {}).get("granted_credits_month")
            if monthly_grant is not None:
                await grant_credits_in_transaction(
                    conn,
                    workspace_id=payment["workspace_id"],
                    amount=Decimal(str(monthly_grant)),
                    source="subscription",
                    idempotency_key=f"subscription:{payment['subscription_id']}:{start.isoformat()}",
                    period_start=start,
                    expires_at=end,
                    subscription_id=payment["subscription_id"],
                )
        if order:
            await conn.execute("UPDATE bill_order SET status='succeeded',version=version+1,updated_at=now() WHERE id=%s", (order["id"],))
        await conn.execute(
            """
            INSERT INTO bill_invoice(id,workspace_id,payment_id,order_id,invoice_number,status,amount,currency)
            VALUES (%s,%s,%s,%s,%s,'issued',%s,%s)
            ON CONFLICT(payment_id) DO UPDATE SET order_id=COALESCE(bill_invoice.order_id,EXCLUDED.order_id)
            """,
            (new_id("inv"), payment["workspace_id"], payment_id, order["id"] if order else None, f"WMA-{payment_id[-12:]}", payment["amount"], payment["currency"]),
        )
    elif outcome == "failed":
        if payment.get("subscription_id"):
            await conn.execute("UPDATE bill_subscription SET pending_plan_code=NULL,updated_at=now() WHERE id=%s", (payment["subscription_id"],))
        if order:
            await conn.execute("UPDATE bill_order SET status='failed',version=version+1,updated_at=now() WHERE id=%s", (order["id"],))
    elif outcome in {"unknown", "charged_back", "refunded"}:
        if order:
            await conn.execute("UPDATE bill_order SET status=%s,version=version+1,updated_at=now() WHERE id=%s", (outcome, order["id"]))
    else:
        raise HTTPException(status_code=422, detail="Unsupported payment event outcome")
    await conn.execute(
        "UPDATE bill_payment SET status=%s,provider_event_id=%s,updated_at=now() WHERE id=%s",
        (status_value, provider_event_id, payment_id),
    )
    await conn.execute(
        "INSERT INTO bill_payment_event(id,provider,provider_event_id,payment_id,payload_hash,outcome,applied_at) VALUES (%s,%s,%s,%s,%s,%s,now())",
        (new_id("pevt"), provider, provider_event_id, payment_id, payload_hash, outcome),
    )
    org_result = await conn.execute("SELECT org_id FROM id_workspace WHERE id=%s", (payment["workspace_id"],))
    org_row = await org_result.fetchone()
    await append_audit_chain(
        conn,
        event_id=new_id("aud"), org_id=org_row["org_id"] if org_row else "", workspace_id=payment["workspace_id"], actor_user_id=None,
        action=f"billing.payment.{outcome}", resource_type="payment", resource_id=payment_id,
        details={"provider": provider, "provider_event_id": provider_event_id, "order_id": payment.get("order_id") or ""},
    )
    final_result = await conn.execute("SELECT * FROM bill_payment WHERE id=%s", (payment_id,))
    return {"payment": await final_result.fetchone(), "replayed": False, "provider_event_id": provider_event_id}


async def _ensure_subscription(conn, actor: Actor) -> dict[str, Any]:
    result = await conn.execute(
        "SELECT s.*, p.name AS plan_name, p.monthly_price, p.currency, p.quotas FROM bill_subscription s JOIN bill_plan p ON p.code=s.plan_code WHERE s.workspace_id=%s",
        (actor.workspace_id,),
    )
    row = await result.fetchone()
    if row:
        return row
    await conn.execute(
        "INSERT INTO bill_subscription(id,org_id,workspace_id,plan_code,current_period_start,current_period_end) VALUES (%s,%s,%s,'free',now(),now()+interval '30 days')",
        (new_id("sub"), actor.org_id, actor.workspace_id),
    )
    result = await conn.execute(
        "SELECT s.*, p.name AS plan_name, p.monthly_price, p.currency, p.quotas FROM bill_subscription s JOIN bill_plan p ON p.code=s.plan_code WHERE s.workspace_id=%s",
        (actor.workspace_id,),
    )
    return await result.fetchone()


def _subscription_summary(row: dict[str, Any]) -> dict[str, Any]:
    return {
        "id": row["id"], "workspace_id": row["workspace_id"], "plan_code": row["plan_code"],
        "plan_name": row["plan_name"], "status": row["status"],
        "current_period_start": row["current_period_start"], "current_period_end": row["current_period_end"],
        "cancel_at_period_end": row["cancel_at_period_end"], "pending_plan_code": row.get("pending_plan_code"),
        "monthly_price": row["monthly_price"], "currency": row["currency"], "quotas": row["quotas"],
    }


@router.get("/plans")
async def list_plans(actor: Annotated[Actor, Depends(get_actor)]):
    async with pool.connection() as conn:
        result = await conn.execute("SELECT code,name,monthly_price,currency,quotas FROM bill_plan WHERE active=TRUE ORDER BY monthly_price,code")
        rows = await result.fetchall()
    return {"items": rows, "data": rows, "next_cursor": None, "has_more": False, "meta": {"request_id": None}}


@router.get("/subscription")
async def get_subscription(actor: Annotated[Actor, Depends(get_actor)]):
    async with pool.connection() as conn:
        row = await _ensure_subscription(conn, actor)
        await conn.commit()
    return _subscription_summary(row)


@router.get("/entitlements")
async def entitlements(actor: Annotated[Actor, Depends(get_actor)]):
    subscription = await get_subscription(actor)
    return {"plan_code": subscription["plan_code"], "status": subscription["status"], "quotas": subscription["quotas"], "valid_until": subscription["current_period_end"]}


@router.post("/subscription/checkout", status_code=status.HTTP_201_CREATED)
async def checkout(body: CheckoutRequest, actor: Annotated[Actor, Depends(get_actor)]):
    _owner(actor)
    async with pool.connection() as conn:
        subscription = await _ensure_subscription(conn, actor)
        order, payment, replayed = await _create_order_in_transaction(
            conn, actor, OrderCreateRequest(
                order_type="subscription", plan_code=body.plan_code,
                provider=body.provider, idempotency_key=body.idempotency_key,
            ),
        )
        await conn.commit()
    return {
        "order": order, "payment": payment,
        "subscription": {**_subscription_summary(subscription), "pending_plan_code": body.plan_code},
        "replayed": replayed,
    }


@router.post("/payments/{payment_id}/confirm", deprecated=True)
async def confirm_payment(
    payment_id: str,
    body: ConfirmPaymentRequest,
    actor: Annotated[Actor, Depends(get_actor)],
    x_provider_signature: Annotated[str | None, Header()] = None,
):
    _owner(actor)
    canonical = json_dumps({
        "payment_id": payment_id,
        "provider_event_id": body.provider_event_id,
        "outcome": body.outcome,
    }).encode()
    if settings.workama_env.lower() == "production" or not x_provider_signature or not hmac.compare_digest(_mock_signature(canonical), x_provider_signature.removeprefix("sha256=")):
        raise HTTPException(status_code=401, detail="Provider signature is required; use the provider callback endpoint")
    async with pool.connection() as conn:
        result = await conn.execute("SELECT * FROM bill_payment WHERE id=%s AND workspace_id=%s", (payment_id, actor.workspace_id))
        payment = await result.fetchone()
        if not payment:
            raise HTTPException(status_code=404, detail="Payment not found")
        applied = await _apply_payment_event(
            conn, provider="mock", payment_id=payment_id,
            provider_event_id=body.provider_event_id, outcome=body.outcome,
            payload_hash=hashlib.sha256(canonical).hexdigest(),
            amount=payment["amount"], currency=payment["currency"],
        )
        await conn.commit()
        final_result = await conn.execute("SELECT * FROM bill_payment WHERE id=%s", (payment_id,))
        final_payment = await final_result.fetchone()
        subscription_result = await conn.execute("SELECT s.*,p.name AS plan_name,p.monthly_price,p.currency,p.quotas FROM bill_subscription s JOIN bill_plan p ON p.code=s.plan_code WHERE s.workspace_id=%s", (actor.workspace_id,))
        subscription = await subscription_result.fetchone()
    return {"payment": final_payment, "subscription": _subscription_summary(subscription), "replayed": applied["replayed"]}


@router.post("/subscription/cancel")
async def cancel_subscription(actor: Annotated[Actor, Depends(get_actor)]):
    _owner(actor)
    async with pool.connection() as conn:
        subscription = await _ensure_subscription(conn, actor)
        await conn.execute("UPDATE bill_subscription SET cancel_at_period_end=TRUE,updated_at=now() WHERE id=%s", (subscription["id"],))
        await conn.commit()
    return {"cancel_at_period_end": True, "effective_at": subscription["current_period_end"]}


@router.post("/subscription/resume")
async def resume_subscription(actor: Annotated[Actor, Depends(get_actor)]):
    _owner(actor)
    async with pool.connection() as conn:
        subscription = await _ensure_subscription(conn, actor)
        await conn.execute("UPDATE bill_subscription SET cancel_at_period_end=FALSE,updated_at=now() WHERE id=%s", (subscription["id"],))
        await conn.commit()
    return {"cancel_at_period_end": False}


@router.get("/invoices")
async def list_invoices(actor: Annotated[Actor, Depends(get_actor)]):
    async with pool.connection() as conn:
        result = await conn.execute("SELECT id,invoice_number,payment_id,status,amount,currency,issued_at FROM bill_invoice WHERE workspace_id=%s ORDER BY issued_at DESC LIMIT 100", (actor.workspace_id,))
        rows = await result.fetchall()
        return {"items": rows, "data": rows, "next_cursor": None, "has_more": False, "meta": {"request_id": None}}

@router.get("/price-catalog")
async def price_catalog(actor: Annotated[Actor, Depends(get_actor)]):
    async with pool.connection() as conn:
        result = await conn.execute(
            """
            SELECT pc.id,pc.plan_code,p.name,pc.region,pc.currency,pc.tax_mode,pc.price,
                   pc.tax_rate,pc.version,pc.effective_from,pc.effective_to
            FROM bill_price_catalog pc JOIN bill_plan p ON p.code=pc.plan_code
            WHERE pc.active=TRUE ORDER BY pc.price,pc.plan_code,pc.version DESC
            """,
        )
        rows = await result.fetchall()
        return {"items": rows, "data": rows, "next_cursor": None, "has_more": False, "meta": {"request_id": None}, "snapshot_policy": "order price_snapshot is immutable"}

@router.get("/orders")
async def list_orders(actor: Annotated[Actor, Depends(get_actor)], limit: int = 50):
    async with pool.connection() as conn:
        result = await conn.execute(
            "SELECT * FROM bill_order WHERE workspace_id=%s ORDER BY created_at DESC LIMIT %s",
            (actor.workspace_id, min(max(limit, 1), 100)),
        )
        rows = [_order_summary(row) for row in await result.fetchall()]
        return {"items": rows, "data": rows, "next_cursor": None, "has_more": False, "meta": {"request_id": None}}

@router.post("/orders", status_code=status.HTTP_201_CREATED)
async def create_order(body: OrderCreateRequest, actor: Annotated[Actor, Depends(get_actor)]):
    _owner(actor)
    async with pool.connection() as conn:
        order, payment, replayed = await _create_order_in_transaction(conn, actor, body)
        await conn.commit()
    return {
        "order": order,
        "payment": payment,
        "replayed": replayed,
        "provider_execution": "local_mock" if body.provider == "mock" else "pending_external",
    }


@router.get("/orders/{order_id}")
async def get_order(order_id: str, actor: Annotated[Actor, Depends(get_actor)]):
    async with pool.connection() as conn:
        row = await _order_with_payment(conn, actor.workspace_id, order_id)
    if not row:
        raise HTTPException(status_code=404, detail="Order not found")
    return {
        "order": _order_summary(row),
        "payment": {key: row.get(key) for key in ("payment_id", "provider", "provider_event_id", "payment_status", "payment_amount", "payment_currency")},
    }


@router.post("/orders/{order_id}/cancellations")
async def cancel_order(order_id: str, body: OrderCancelRequest, actor: Annotated[Actor, Depends(get_actor)]):
    _owner(actor)
    _require_step_up(actor, "Order cancellation")
    async with pool.connection() as conn:
        result = await conn.execute("SELECT * FROM bill_order WHERE id=%s AND workspace_id=%s FOR UPDATE", (order_id, actor.workspace_id))
        order = await result.fetchone()
        if not order:
            raise HTTPException(status_code=404, detail="Order not found")
        if order["status"] == "canceled":
            await conn.commit()
            return {"order": _order_summary(order), "replayed": True}
        if order["status"] not in {"pending", "unknown"}:
            raise HTTPException(status_code=409, detail="Order can no longer be canceled")
        await conn.execute("UPDATE bill_order SET status='canceled',version=version+1,updated_at=now() WHERE id=%s", (order_id,))
        payment_result = await conn.execute("SELECT * FROM bill_payment WHERE order_id=%s FOR UPDATE", (order_id,))
        payment = await payment_result.fetchone()
        if payment and payment["status"] in {"pending", "unknown"}:
            await conn.execute("UPDATE bill_payment SET status='failed',updated_at=now() WHERE id=%s", (payment["id"],))
            if payment.get("subscription_id"):
                await conn.execute("UPDATE bill_subscription SET pending_plan_code=NULL,updated_at=now() WHERE id=%s", (payment["subscription_id"],))
        await append_audit_chain(
            conn, event_id=new_id("aud"), org_id=actor.org_id, workspace_id=actor.workspace_id,
            actor_user_id=actor.user_id, action="billing.order.canceled", resource_type="order",
            resource_id=order_id, details={"reason": body.reason, "idempotency_key": body.idempotency_key},
        )
        await conn.commit()
        final = await conn.execute("SELECT * FROM bill_order WHERE id=%s", (order_id,))
        final_order = await final.fetchone()
    return {"order": _order_summary(final_order), "replayed": False}


@router.get("/payment-methods")
async def list_payment_methods(actor: Annotated[Actor, Depends(get_actor)]):
    async with pool.connection() as conn:
        result = await conn.execute(
            "SELECT id,provider,method_type,display_label,status,created_at,detached_at FROM bill_payment_method WHERE workspace_id=%s ORDER BY created_at DESC",
            (actor.workspace_id,),
        )
        rows = await result.fetchall()
        return {"items": rows, "data": rows, "next_cursor": None, "has_more": False, "meta": {"request_id": None}, "secret_storage": "hash_only"}

@router.post("/payment-method-setups", status_code=status.HTTP_201_CREATED)
async def create_payment_method_setup(body: PaymentMethodSetupRequest, actor: Annotated[Actor, Depends(get_actor)]):
    _owner(actor)
    if body.provider != "mock":
        return {"status": "pending_external", "provider": body.provider, "provider_execution": "pending_external"}
    token_hash = hash_secret(body.token)
    async with pool.connection() as conn:
        result = await conn.execute(
            """
            INSERT INTO bill_payment_method(id,workspace_id,provider,method_type,display_label,provider_ref_hash,created_by)
            VALUES (%s,%s,%s,%s,%s,%s,%s)
            ON CONFLICT(workspace_id,provider,provider_ref_hash) DO UPDATE SET status='active',detached_at=NULL,detach_reason=NULL
            RETURNING id,provider,method_type,display_label,status,created_at,detached_at
            """,
            (new_id("pm"), actor.workspace_id, body.provider, body.method_type, body.display_label, token_hash, actor.user_id),
        )
        method = await result.fetchone()
        await conn.commit()
    return {"status": "active", "payment_method": method, "secret_storage": "hash_only"}


@router.delete("/payment-methods/{payment_method_id}")
async def delete_payment_method(payment_method_id: str, body: DeletePaymentMethodRequest, actor: Annotated[Actor, Depends(get_actor)]):
    _owner(actor)
    _require_step_up(actor, "Payment method removal")
    async with pool.connection() as conn:
        result = await conn.execute(
            "UPDATE bill_payment_method SET status='detached',detached_at=now(),detach_reason=%s WHERE id=%s AND workspace_id=%s AND status='active' RETURNING id",
            (body.reason, payment_method_id, actor.workspace_id),
        )
        row = await result.fetchone()
        if not row:
            raise HTTPException(status_code=404, detail="Active payment method not found")
        await append_audit_chain(
            conn, event_id=new_id("aud"), org_id=actor.org_id, workspace_id=actor.workspace_id,
            actor_user_id=actor.user_id, action="billing.payment_method.detached", resource_type="payment_method",
            resource_id=payment_method_id, details={"reason": body.reason},
        )
        await conn.commit()
    return {"id": payment_method_id, "status": "detached"}


@router.post("/providers/{provider}/callbacks")
async def handle_payment_provider_callback(provider: str, request: Request):
    if provider not in {"mock", "wechat", "alipay", "stripe"}:
        raise HTTPException(status_code=404, detail="Unsupported payment provider")
    secret = settings.billing_mock_webhook_secret if provider == "mock" else ""
    if not secret:
        raise HTTPException(status_code=503, detail="pending_external: provider signature verifier is not configured")
    raw = await request.body()
    if len(raw) > 65536:
        raise HTTPException(status_code=413, detail="Provider callback payload is too large")
    supplied = request.headers.get("x-provider-signature", "").removeprefix("sha256=")
    expected = hmac.new(secret.encode(), raw, hashlib.sha256).hexdigest()
    if not supplied or not hmac.compare_digest(expected, supplied):
        raise HTTPException(status_code=401, detail="Invalid provider signature")
    try:
        payload = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise HTTPException(status_code=400, detail="Provider callback must be valid JSON") from exc
    if not isinstance(payload, dict):
        raise HTTPException(status_code=400, detail="Provider callback must be an object")
    event_id = str(payload.get("event_id") or request.headers.get("x-provider-event-id") or "")
    payment_id = str(payload.get("payment_id") or "")
    outcome = str(payload.get("status") or payload.get("outcome") or "")
    if len(event_id) < 8 or not payment_id or outcome not in {"succeeded", "failed", "unknown", "charged_back", "refunded"}:
        raise HTTPException(status_code=422, detail="Provider callback is missing a valid event_id, payment_id or status")
    async with pool.connection() as conn:
        applied = await _apply_payment_event(
            conn, provider=provider, payment_id=payment_id, provider_event_id=event_id,
            outcome=outcome, payload_hash=hashlib.sha256(raw).hexdigest(),
            amount=payload.get("amount"), currency=payload.get("currency"),
        )
        await conn.commit()
    return {"accepted": True, "provider": provider, **applied}


@router.post("/subscription-change-previews")
async def preview_subscription_change(body: SubscriptionChangeRequest, actor: Annotated[Actor, Depends(get_actor)]):
    target = _plan(body.plan_code)
    async with pool.connection() as conn:
        current = await _ensure_subscription(conn, actor)
        await conn.commit()
    difference = Decimal(str(target["monthly_price"])) - Decimal(str(current["monthly_price"]))
    return {
        "current_plan_code": current["plan_code"], "target_plan_code": body.plan_code,
        "current_price": current["monthly_price"], "target_price": target["monthly_price"],
        "price_difference": difference, "currency": target["currency"],
        "effective_at": current["current_period_end"], "payment_required": difference > 0,
        "price_snapshot_version": 1,
    }


@router.post("/subscription-changes", status_code=status.HTTP_201_CREATED)
async def change_subscription(body: SubscriptionChangeConfirm, actor: Annotated[Actor, Depends(get_actor)]):
    _owner(actor)
    _require_step_up(actor, "Subscription change")
    async with pool.connection() as conn:
        order, payment, replayed = await _create_order_in_transaction(
            conn, actor, OrderCreateRequest(order_type="subscription", plan_code=body.plan_code, provider=body.provider, idempotency_key=body.idempotency_key),
        )
        await conn.commit()
    return {"order": order, "payment": payment, "replayed": replayed, "provider_execution": "local_mock" if body.provider == "mock" else "pending_external"}


@router.post("/subscription-cancellations")
async def cancel_subscription_v2(actor: Annotated[Actor, Depends(get_actor)]):
    _owner(actor)
    _require_step_up(actor, "Subscription cancellation")
    async with pool.connection() as conn:
        subscription = await _ensure_subscription(conn, actor)
        await conn.execute("UPDATE bill_subscription SET cancel_at_period_end=TRUE,updated_at=now() WHERE id=%s", (subscription["id"],))
        await conn.commit()
    return {"subscription_id": subscription["id"], "cancel_at_period_end": True, "effective_at": subscription["current_period_end"]}


@router.post("/subscription-resumptions")
async def resume_subscription_v2(actor: Annotated[Actor, Depends(get_actor)]):
    _owner(actor)
    _require_step_up(actor, "Subscription resumption")
    async with pool.connection() as conn:
        subscription = await _ensure_subscription(conn, actor)
        await conn.execute("UPDATE bill_subscription SET cancel_at_period_end=FALSE,updated_at=now() WHERE id=%s", (subscription["id"],))
        await conn.commit()
    return {"subscription_id": subscription["id"], "cancel_at_period_end": False}


@router.post("/invoice-requests", status_code=status.HTTP_202_ACCEPTED)
async def create_invoice_request(body: InvoiceRequestCreate, actor: Annotated[Actor, Depends(get_actor)]):
    _owner(actor)
    async with pool.connection() as conn:
        existing_result = await conn.execute(
            "SELECT * FROM bill_invoice_request WHERE workspace_id=%s AND idempotency_key=%s",
            (actor.workspace_id, body.idempotency_key),
        )
        existing = await existing_result.fetchone()
        if existing:
            await conn.commit()
            return {"request": existing, "replayed": True, "provider_execution": "pending_external"}
        order_result = await conn.execute("SELECT id,status FROM bill_order WHERE id=%s AND workspace_id=%s", (body.order_id, actor.workspace_id))
        order = await order_result.fetchone()
        if not order:
            raise HTTPException(status_code=404, detail="Order not found")
        if order["status"] != "succeeded":
            raise HTTPException(status_code=409, detail="Only succeeded orders can request an invoice")
        result = await conn.execute(
            "INSERT INTO bill_invoice_request(id,workspace_id,order_id,tax_profile,idempotency_key,created_by) VALUES (%s,%s,%s,%s::jsonb,%s,%s) RETURNING *",
            (new_id("invr"), actor.workspace_id, body.order_id, json_dumps(body.tax_profile), body.idempotency_key, actor.user_id),
        )
        request_row = await result.fetchone()
        await conn.commit()
    return {"request": request_row, "replayed": False, "provider_execution": "pending_external"}


@router.get("/invoices/{invoice_id}")
async def get_invoice(invoice_id: str, actor: Annotated[Actor, Depends(get_actor)]):
    async with pool.connection() as conn:
        result = await conn.execute("SELECT * FROM bill_invoice WHERE id=%s AND workspace_id=%s", (invoice_id, actor.workspace_id))
        invoice = await result.fetchone()
    if not invoice:
        raise HTTPException(status_code=404, detail="Invoice not found")
    return {"invoice": invoice, "download": {"available": False, "status": "pending_external"}}


@router.post("/invoices/{invoice_id}/downloads", status_code=status.HTTP_202_ACCEPTED)
async def create_invoice_download(invoice_id: str, actor: Annotated[Actor, Depends(get_actor)]):
    async with pool.connection() as conn:
        result = await conn.execute("SELECT id FROM bill_invoice WHERE id=%s AND workspace_id=%s", (invoice_id, actor.workspace_id))
        invoice = await result.fetchone()
    if not invoice:
        raise HTTPException(status_code=404, detail="Invoice not found")
    return {"invoice_id": invoice_id, "status": "pending_external", "download_url": None, "provider_execution": "pending_external"}


@router.get("/refunds")
async def list_refunds(actor: Annotated[Actor, Depends(get_actor)], limit: int = 50):
    async with pool.connection() as conn:
        result = await conn.execute(
            "SELECT id,payment_id,order_id,amount,currency,reason,status,approval_ref,created_at,updated_at FROM bill_refund WHERE workspace_id=%s ORDER BY created_at DESC LIMIT %s",
            (actor.workspace_id, min(max(limit, 1), 100)),
        )
        rows = await result.fetchall()
        return {"items": rows, "data": rows, "next_cursor": None, "has_more": False, "meta": {"request_id": None}}

@router.post("/refunds", status_code=status.HTTP_201_CREATED)
async def create_refund(body: RefundCreateRequest, actor: Annotated[Actor, Depends(get_actor)]):
    _owner(actor)
    _require_step_up(actor, "Refund")
    async with pool.connection() as conn:
        existing_result = await conn.execute("SELECT * FROM bill_refund WHERE workspace_id=%s AND idempotency_key=%s", (actor.workspace_id, body.idempotency_key))
        existing = await existing_result.fetchone()
        if existing:
            await conn.commit()
            return {"refund": existing, "replayed": True}
        if body.payment_id:
            payment_result = await conn.execute("SELECT * FROM bill_payment WHERE id=%s AND workspace_id=%s FOR UPDATE", (body.payment_id, actor.workspace_id))
        else:
            payment_result = await conn.execute("SELECT p.* FROM bill_payment p JOIN bill_order o ON o.id=p.order_id WHERE o.id=%s AND o.workspace_id=%s FOR UPDATE", (body.order_id, actor.workspace_id))
        payment = await payment_result.fetchone()
        if not payment:
            raise HTTPException(status_code=404, detail="Payment not found")
        if payment["status"] != "succeeded":
            raise HTTPException(status_code=409, detail="Only succeeded payments can be refunded")
        order = None
        if payment.get("order_id"):
            order_result = await conn.execute("SELECT * FROM bill_order WHERE id=%s AND workspace_id=%s FOR UPDATE", (payment["order_id"], actor.workspace_id))
            order = await order_result.fetchone()
        total_result = await conn.execute("SELECT COALESCE(SUM(amount),0) AS total FROM bill_refund WHERE payment_id=%s AND status='succeeded'", (payment["id"],))
        total = Decimal(str((await total_result.fetchone())["total"]))
        remaining = Decimal(str(payment["amount"])) - total
        amount = (body.amount or remaining).quantize(Decimal("0.01"))
        if amount <= 0 or amount > remaining:
            raise HTTPException(status_code=409, detail="Refund amount exceeds the remaining refundable amount")
        refund_id = new_id("rfd")
        refund_status = "succeeded" if payment["provider"] == "mock" else "pending_external"
        result = await conn.execute(
            "INSERT INTO bill_refund(id,workspace_id,payment_id,order_id,amount,currency,reason,status,provider_ref_hash,idempotency_key,approval_ref,created_by) VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s) RETURNING *",
            (refund_id, actor.workspace_id, payment["id"], payment.get("order_id"), amount, payment["currency"], body.reason, refund_status, hash_secret(f"{payment['provider']}:{refund_id}"), body.idempotency_key, body.approval_ref, actor.user_id),
        )
        refund = await result.fetchone()
        if refund_status == "succeeded":
            if order and order["order_type"] == "credits" and order["credits"]:
                refund_credits = (order["credits"] * amount / Decimal(str(payment["amount"]))).quantize(Decimal("0.000001"))
                account_result = await conn.execute("SELECT id,granted_balance,purchased_balance,frozen_balance FROM bill_account WHERE workspace_id=%s FOR UPDATE", (actor.workspace_id,))
                account = await account_result.fetchone()
                if not account or account["purchased_balance"] < refund_credits:
                    raise HTTPException(status_code=409, detail="Purchased balance is insufficient for this refund reversal")
                balance_after = account["granted_balance"] + account["purchased_balance"] - refund_credits
                await conn.execute("UPDATE bill_account SET purchased_balance=purchased_balance-%s,version=version+1,updated_at=now() WHERE id=%s", (refund_credits, account["id"]))
                await conn.execute("INSERT INTO bill_transaction(id,workspace_id,kind,amount,balance_after,reference_id,description) VALUES (%s,%s,'refund',%s,%s,%s,%s) ON CONFLICT(workspace_id,reference_id,kind) DO NOTHING", (new_id("txn"), actor.workspace_id, -refund_credits, balance_after, refund_id, "Credit order refund"))
            new_total = total + amount
            final_status = "refunded" if new_total >= Decimal(str(payment["amount"])) else "partially_refunded"
            await conn.execute("UPDATE bill_payment SET status=%s,updated_at=now() WHERE id=%s", ("refunded" if final_status == "refunded" else "succeeded", payment["id"]))
            if order:
                await conn.execute("UPDATE bill_order SET status=%s,version=version+1,updated_at=now() WHERE id=%s", (final_status, order["id"]))
            if final_status == "refunded":
                await conn.execute("UPDATE bill_invoice SET status='credited' WHERE payment_id=%s", (payment["id"],))
        await append_audit_chain(
            conn, event_id=new_id("aud"), org_id=actor.org_id, workspace_id=actor.workspace_id,
            actor_user_id=actor.user_id, action="billing.refund.created", resource_type="refund",
            resource_id=refund_id, details={"payment_id": payment["id"], "amount": str(amount), "reason": body.reason, "status": refund_status},
        )
        await conn.commit()
    return {"refund": refund, "replayed": False, "provider_execution": "local_mock" if refund_status == "succeeded" else "pending_external"}


@router.get("/refunds/{refund_id}")
async def get_refund(refund_id: str, actor: Annotated[Actor, Depends(get_actor)]):
    async with pool.connection() as conn:
        result = await conn.execute("SELECT * FROM bill_refund WHERE id=%s AND workspace_id=%s", (refund_id, actor.workspace_id))
        refund = await result.fetchone()
    if not refund:
        raise HTTPException(status_code=404, detail="Refund not found")
    return {"refund": refund}
