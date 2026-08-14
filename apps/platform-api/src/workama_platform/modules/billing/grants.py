from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal

from workama_platform.core import new_id, pool


GRANT_QUANTUM = Decimal("0.000001")


def month_period(now: datetime | None = None) -> tuple[datetime, datetime]:
    current = (now or datetime.now(UTC)).astimezone(UTC)
    start = current.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
    if start.month == 12:
        end = start.replace(year=start.year + 1, month=1)
    else:
        end = start.replace(month=start.month + 1)
    return start, end


def quantize_credits(value: Decimal | int | str) -> Decimal:
    return Decimal(str(value)).quantize(GRANT_QUANTUM)


async def grant_credits_in_transaction(
    conn,
    *,
    workspace_id: str,
    amount: Decimal,
    source: str,
    idempotency_key: str,
    period_start: datetime,
    expires_at: datetime | None,
    subscription_id: str | None = None,
) -> dict:
    amount = quantize_credits(amount)
    if amount <= 0:
        raise ValueError("Credit grant amount must be positive")

    existing_result = await conn.execute(
        "SELECT * FROM bill_credit_grant WHERE workspace_id=%s AND idempotency_key=%s FOR UPDATE",
        (workspace_id, idempotency_key),
    )
    existing = await existing_result.fetchone()
    if existing:
        return existing

    account_result = await conn.execute(
        "SELECT id,granted_balance,purchased_balance FROM bill_account WHERE workspace_id=%s FOR UPDATE",
        (workspace_id,),
    )
    account = await account_result.fetchone()
    if not account:
        raise ValueError("Billing account missing")

    grant_result = await conn.execute(
        """
        INSERT INTO bill_credit_grant(
          id,workspace_id,subscription_id,source,period_start,expires_at,
          initial_amount,remaining_amount,status,idempotency_key
        ) VALUES (%s,%s,%s,%s,%s,%s,%s,%s,'active',%s)
        ON CONFLICT(workspace_id,idempotency_key) DO NOTHING
        RETURNING *
        """,
        (
            new_id("grant"), workspace_id, subscription_id, source,
            period_start, expires_at, amount, amount, idempotency_key,
        ),
    )
    grant = await grant_result.fetchone()
    if not grant:
        replay = await conn.execute(
            "SELECT * FROM bill_credit_grant WHERE workspace_id=%s AND idempotency_key=%s",
            (workspace_id, idempotency_key),
        )
        return await replay.fetchone()

    balance_after = account["granted_balance"] + account["purchased_balance"] + amount
    await conn.execute(
        "UPDATE bill_account SET granted_balance=granted_balance+%s,version=version+1,updated_at=now() WHERE id=%s",
        (amount, account["id"]),
    )
    await conn.execute(
        """
        INSERT INTO bill_transaction(
          id,workspace_id,kind,amount,balance_after,reference_id,description
        ) VALUES (%s,%s,'grant',%s,%s,%s,%s)
        ON CONFLICT(workspace_id,reference_id,kind) DO NOTHING
        """,
        (new_id("txn"), workspace_id, amount, balance_after, grant["id"], f"{source} credit grant"),
    )
    return {**grant, "balance_after": balance_after}


async def consume_granted_credits_in_transaction(conn, workspace_id: str, amount: Decimal) -> Decimal:
    remaining = quantize_credits(amount)
    if remaining <= 0:
        return Decimal("0")
    result = await conn.execute(
        """
        SELECT id,remaining_amount
        FROM bill_credit_grant
        WHERE workspace_id=%s AND status='active' AND remaining_amount>0
        ORDER BY expires_at NULLS LAST, period_start, created_at, id
        FOR UPDATE
        """,
        (workspace_id,),
    )
    consumed = Decimal("0")
    for row in await result.fetchall():
        if remaining <= 0:
            break
        used = min(remaining, row["remaining_amount"])
        after = row["remaining_amount"] - used
        await conn.execute(
            "UPDATE bill_credit_grant SET remaining_amount=%s,status=%s WHERE id=%s",
            (after, "exhausted" if after == 0 else "active", row["id"]),
        )
        consumed += used
        remaining -= used
    if remaining > 0:
        raise ValueError("Granted credit buckets are inconsistent with bill_account")
    return consumed


async def expire_credit_grants_in_transaction(conn, workspace_id: str | None = None) -> list[dict]:
    query = """
        SELECT id,workspace_id,remaining_amount
        FROM bill_credit_grant
        WHERE status='active' AND expires_at IS NOT NULL AND expires_at<=now()
    """
    params: tuple[str, ...] = ()
    if workspace_id:
        query += " AND workspace_id=%s"
        params = (workspace_id,)
    query += " ORDER BY workspace_id,id FOR UPDATE"
    result = await conn.execute(query, params)
    expired: list[dict] = []
    accounts: dict[str, dict] = {}
    for grant in await result.fetchall():
        current_account = accounts.get(grant["workspace_id"])
        if current_account is None:
            account_result = await conn.execute(
                "SELECT id,granted_balance,purchased_balance FROM bill_account WHERE workspace_id=%s FOR UPDATE",
                (grant["workspace_id"],),
            )
            current_account = await account_result.fetchone()
            if not current_account:
                raise ValueError("Billing account missing for credit grant")
            accounts[grant["workspace_id"]] = current_account

        amount = quantize_credits(grant["remaining_amount"])
        if amount > current_account["granted_balance"]:
            raise ValueError("Granted credit bucket exceeds account balance")
        current_account["granted_balance"] -= amount
        await conn.execute(
            "UPDATE bill_credit_grant SET remaining_amount=0,status='expired',expired_at=now() WHERE id=%s",
            (grant["id"],),
        )
        if amount > 0:
            balance_after = current_account["granted_balance"] + current_account["purchased_balance"]
            await conn.execute(
                "UPDATE bill_account SET granted_balance=%s,version=version+1,updated_at=now() WHERE id=%s",
                (current_account["granted_balance"], current_account["id"]),
            )
            await conn.execute(
                """
                INSERT INTO bill_transaction(
                  id,workspace_id,kind,amount,balance_after,reference_id,description
                ) VALUES (%s,%s,'expire',%s,%s,%s,'Expired granted credits')
                ON CONFLICT(workspace_id,reference_id,kind) DO NOTHING
                """,
                (new_id("txn"), grant["workspace_id"], -amount, balance_after, grant["id"]),
            )
        expired.append({"grant_id": grant["id"], "workspace_id": grant["workspace_id"], "expired_credits": amount})
    return expired


async def expire_credit_grants(workspace_id: str | None = None) -> list[dict]:
    async with pool.connection() as conn:
        async with conn.transaction():
            return await expire_credit_grants_in_transaction(conn, workspace_id)
