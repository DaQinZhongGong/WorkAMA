from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal

from workama_platform.core import new_id, pool


@dataclass(frozen=True)
class ReconciliationResult:
    usage_credits: Decimal
    ledger_credits: Decimal
    difference: Decimal
    difference_ratio: Decimal
    status: str


def hour_bucket(value: datetime) -> datetime:
    return value.astimezone(UTC).replace(minute=0, second=0, microsecond=0)


def reconcile_totals(usage_credits: Decimal, ledger_credits: Decimal) -> ReconciliationResult:
    usage = usage_credits.quantize(Decimal("0.000001"))
    ledger = ledger_credits.quantize(Decimal("0.000001"))
    difference = abs(usage - ledger).quantize(Decimal("0.000001"))
    denominator = max(abs(usage), abs(ledger))
    ratio = Decimal("0") if denominator == 0 else difference / denominator
    ratio = ratio.quantize(Decimal("0.000001"))
    return ReconciliationResult(usage, ledger, difference, ratio, "passed" if ratio <= Decimal("0.001") else "mismatch")


async def run_daily_reconciliation(business_date: date, workspace_id: str | None = None) -> list[dict]:
    start = datetime.combine(business_date, datetime.min.time(), tzinfo=UTC)
    end = start + timedelta(days=1)
    results: list[dict] = []
    async with pool.connection() as conn:
        async with conn.transaction():
            if workspace_id:
                workspace_rows = await conn.execute(
                    "SELECT workspace_id FROM bill_account WHERE workspace_id = %s",
                    (workspace_id,),
                )
            else:
                workspace_rows = await conn.execute("SELECT workspace_id FROM bill_account")
            for row in await workspace_rows.fetchall():
                current_workspace = row["workspace_id"]
                usage_row = await (await conn.execute(
                    "SELECT COALESCE(SUM(cost_credits), 0) AS total FROM bill_usage_record WHERE workspace_id = %s AND created_at >= %s AND created_at < %s",
                    (current_workspace, start, end),
                )).fetchone()
                ledger_row = await (await conn.execute(
                    "SELECT COALESCE(-SUM(amount), 0) AS total FROM bill_transaction WHERE workspace_id = %s AND kind = 'usage' AND created_at >= %s AND created_at < %s",
                    (current_workspace, start, end),
                )).fetchone()
                result = reconcile_totals(Decimal(usage_row["total"]), Decimal(ledger_row["total"]))
                run_id = new_id("rec")
                saved = await conn.execute(
                    """
                    INSERT INTO bill_reconciliation_run(
                        id, workspace_id, business_date, usage_credits, ledger_credits,
                        difference, difference_ratio, status, checked_at
                    ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, now())
                    ON CONFLICT(workspace_id, business_date) DO UPDATE SET
                        usage_credits = EXCLUDED.usage_credits,
                        ledger_credits = EXCLUDED.ledger_credits,
                        difference = EXCLUDED.difference,
                        difference_ratio = EXCLUDED.difference_ratio,
                        status = EXCLUDED.status,
                        checked_at = now()
                    RETURNING id, workspace_id, business_date, usage_credits, ledger_credits,
                              difference, difference_ratio, status, checked_at
                    """,
                    (run_id, current_workspace, business_date, result.usage_credits, result.ledger_credits,
                     result.difference, result.difference_ratio, result.status),
                )
                results.append(await saved.fetchone())
    return results
