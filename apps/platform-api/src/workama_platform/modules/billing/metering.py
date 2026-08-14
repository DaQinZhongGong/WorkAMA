from __future__ import annotations

from datetime import datetime
from decimal import Decimal
from typing import Literal

from fastapi import HTTPException
from pydantic import BaseModel, ConfigDict, Field, model_validator

from workama_platform.core import json_dumps, new_id, pool
from workama_platform.modules.billing.grants import (
    consume_granted_credits_in_transaction,
    expire_credit_grants_in_transaction,
)
from workama_platform.modules.notification.service import create_low_balance_notifications


class MeterRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    request_id: str = Field(min_length=1, max_length=160)
    workspace_id: str | None = Field(default=None, min_length=1, max_length=40)
    token_id: str | None = Field(default=None, max_length=40)
    channel_id: str | None = Field(default=None, max_length=40)
    model: str = Field(min_length=1, max_length=120)
    prompt_tokens: int = Field(default=0, ge=0)
    completion_tokens: int = Field(default=0, ge=0)
    latency_ms: int = Field(default=0, ge=0)
    status_code: int = Field(default=200, ge=100, le=599)
    error_code: str | None = Field(default=None, max_length=40)


class MeteringEvent(BaseModel):
    model_config = ConfigDict(extra="forbid")

    schema_version: Literal[1]
    event_id: str = Field(min_length=1, max_length=80)
    event_type: Literal["metering.llm.v1"]
    occurred_at: datetime
    producer: Literal["gateway"]
    workspace_id: str = Field(min_length=1, max_length=40)
    trace_id: str = Field(min_length=1, max_length=160)
    idempotency_key: str = Field(min_length=1, max_length=160)
    classification: Literal["C2"]
    payload: MeterRequest

    @model_validator(mode="after")
    def validate_references(self):
        if self.idempotency_key != self.payload.request_id:
            raise ValueError("idempotency_key must equal payload.request_id")
        if self.payload.workspace_id not in {None, self.workspace_id}:
            raise ValueError("payload.workspace_id must match workspace_id")
        self.payload.workspace_id = self.workspace_id
        return self


def _assert_workspace_match(
    resource: dict | None, workspace_id: str, resource_name: str,
) -> None:
    """Reject a globally unique request id that belongs to another tenant."""
    if resource and resource.get("workspace_id") != workspace_id:
        raise HTTPException(
            status_code=409,
            detail=f"E00008 {resource_name} belongs to another workspace",
        )


async def settle_meter(body: MeterRequest) -> dict:
    if not body.workspace_id:
        raise HTTPException(status_code=422, detail="workspace_id is required")
    async with pool.connection() as conn:
        async with conn.transaction():
            return await settle_meter_in_transaction(conn, body)


async def settle_meter_in_transaction(conn, body: MeterRequest) -> dict:
    await expire_credit_grants_in_transaction(conn, body.workspace_id)
    account_result = await conn.execute(
        "SELECT id, granted_balance, purchased_balance, frozen_balance FROM bill_account WHERE workspace_id = %s FOR UPDATE",
        (body.workspace_id,),
    )
    account = await account_result.fetchone()
    if not account:
        raise HTTPException(status_code=404, detail="Billing account missing")

    duplicate = await conn.execute(
        "SELECT workspace_id, cost_credits FROM bill_usage_record WHERE request_id = %s",
        (body.request_id,),
    )
    existing = await duplicate.fetchone()
    if existing:
        _assert_workspace_match(existing, body.workspace_id, "Usage request")
        return {"duplicate": True, "cost_credits": existing["cost_credits"]}

    price_result = await conn.execute(
        """
        SELECT input_per_million, output_per_million, markup_percent
        FROM gw_model_price WHERE workspace_id = %s AND model = %s
        """,
        (body.workspace_id, body.model),
    )
    price = await price_result.fetchone() or {
        "input_per_million": Decimal("1"),
        "output_per_million": Decimal("2"),
        "markup_percent": Decimal("10"),
    }
    base = (
        Decimal(body.prompt_tokens) * price["input_per_million"]
        + Decimal(body.completion_tokens) * price["output_per_million"]
    ) / Decimal(1_000_000)
    cost = (base * (Decimal(1) + price["markup_percent"] / Decimal(100))).quantize(
        Decimal("0.000001")
    )
    reservation_result = await conn.execute(
        "SELECT id, workspace_id, estimated_cost, status FROM bill_reservation WHERE request_id = %s FOR UPDATE",
        (body.request_id,),
    )
    reservation = await reservation_result.fetchone()
    _assert_workspace_match(reservation, body.workspace_id, "Reservation")
    reservation_frozen = reservation["estimated_cost"] if reservation and reservation["status"] == "frozen" else Decimal("0")
    frozen_after = account["frozen_balance"] - reservation_frozen
    if frozen_after < 0:
        raise HTTPException(status_code=409, detail="Reservation state is invalid")
    from_granted = min(account["granted_balance"], cost)
    remaining_cost = cost - from_granted
    granted_after = account["granted_balance"] - from_granted
    purchased_after = account["purchased_balance"] - remaining_cost
    if purchased_after < 0:
        raise HTTPException(status_code=402, detail="E01004")
    balance_after = granted_after + purchased_after

    consumed_granted = await consume_granted_credits_in_transaction(
        conn, body.workspace_id, from_granted
    )
    if consumed_granted != from_granted:
        raise HTTPException(status_code=409, detail="Granted credit ledger is inconsistent")

    await conn.execute(
        """
        UPDATE bill_account SET granted_balance = %s, purchased_balance = %s, frozen_balance = %s,
            version = version + 1, updated_at = now() WHERE id = %s
        """,
        (granted_after, purchased_after, frozen_after, account["id"]),
    )
    if reservation and reservation["status"] == "frozen":
        await conn.execute(
            "UPDATE bill_reservation SET status = 'settled', actual_cost = %s, settled_at = now() WHERE id = %s",
            (cost, reservation["id"]),
        )
    await conn.execute(
        """
        INSERT INTO bill_usage_record(
            id, workspace_id, request_id, model, prompt_tokens, completion_tokens, cost_credits
        ) VALUES (%s, %s, %s, %s, %s, %s, %s)
        """,
        (
            new_id("use"), body.workspace_id, body.request_id, body.model,
            body.prompt_tokens, body.completion_tokens, cost,
        ),
    )
    await create_low_balance_notifications(conn, body.workspace_id, balance_after - frozen_after)
    await conn.execute(
        """
        INSERT INTO bill_usage_hourly(
            workspace_id, resource, model, hour, requests,
            prompt_tokens, completion_tokens, cost_credits
        ) VALUES (%s, 'llm', %s, date_trunc('hour', now()), 1, %s, %s, %s)
        ON CONFLICT(workspace_id, resource, model, hour) DO UPDATE SET
            requests = bill_usage_hourly.requests + 1,
            prompt_tokens = bill_usage_hourly.prompt_tokens + EXCLUDED.prompt_tokens,
            completion_tokens = bill_usage_hourly.completion_tokens + EXCLUDED.completion_tokens,
            cost_credits = bill_usage_hourly.cost_credits + EXCLUDED.cost_credits,
            updated_at = now()
        """,
        (body.workspace_id, body.model, body.prompt_tokens, body.completion_tokens, cost),
    )
    await conn.execute(
        """
        INSERT INTO bill_transaction(
            id, workspace_id, kind, amount, balance_after, reference_id, description
        ) VALUES (%s, %s, 'usage', %s, %s, %s, %s)
        """,
        (
            new_id("txn"), body.workspace_id, -cost, balance_after,
            body.request_id, f"{body.model} model usage",
        ),
    )
    await conn.execute(
        """
        INSERT INTO gw_request_log(
            request_id, workspace_id, token_id, channel_id, model,
            prompt_tokens, completion_tokens, total_tokens, cost_credits,
            latency_ms, status_code, error_code
        ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
        """,
        (
            body.request_id, body.workspace_id, body.token_id, body.channel_id,
            body.model, body.prompt_tokens, body.completion_tokens,
            body.prompt_tokens + body.completion_tokens, cost,
            body.latency_ms, body.status_code, body.error_code,
        ),
    )
    return {"duplicate": False, "cost_credits": cost, "balance": balance_after}


async def settle_meter_event(
    event: MeteringEvent, subject: str, consumer_name: str
) -> bool:
    """Idempotently record a metering event in ops_inbox and settle the charge.

    Returns True when the event was newly processed, False when it was already
    recorded by this consumer (duplicate event_id).
    """
    async with pool.connection() as conn:
        async with conn.transaction():
            inserted = await conn.execute(
                """
                INSERT INTO ops_inbox(
                    id, event_id, subject, consumer_name, payload, status, request_id
                ) VALUES (%s, %s, %s, %s, %s::jsonb, 'processing', %s)
                ON CONFLICT(event_id, consumer_name) DO NOTHING
                RETURNING id
                """,
                (
                    new_id("inb"), event.event_id, subject, consumer_name,
                    json_dumps(event.model_dump(mode="json")),
                    event.payload.request_id,
                ),
            )
            inbox = await inserted.fetchone()
            if not inbox:
                return False
            await settle_meter_in_transaction(conn, event.payload)
            await conn.execute(
                """
                UPDATE ops_inbox SET status = 'processed', processed_at = now()
                WHERE id = %s
                """,
                (inbox["id"],),
            )
            return True
