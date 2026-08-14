from datetime import datetime, timezone

import pytest
from fastapi import HTTPException

from workama_platform.core import Actor
from workama_platform.modules.billing import router as billing_router

try:  # pragma: no cover
    from datetime import UTC
except ImportError:  # Python < 3.11 compatibility for the current runtime
    UTC = timezone.utc


def owner(workspace_id: str = "wsp_current") -> Actor:
    return Actor(
        user_id="usr_owner",
        workspace_id=workspace_id,
        org_id="org_test",
        role="owner",
        email="owner@example.test",
        display_name="Owner",
        onboarding_completed=True,
        capabilities=("*",),
    )


def test_billing_routes_cover_contract_drift_endpoints():
    paths = {(route.path, tuple(sorted(route.methods or ()))) for route in billing_router.router.routes}
    assert ("/api/v1/billing/transactions/{transaction_id}", ("GET",)) in paths
    assert ("/api/v1/billing/usage-exports", ("POST",)) in paths
    assert ("/api/v1/billing/forecast", ("GET",)) in paths


def test_usage_export_request_schema_enforces_format_and_bounds():
    request = billing_router.UsageExportRequest(format="csv")
    assert request.format == "csv"
    with pytest.raises(ValueError):
        billing_router.UsageExportRequest(format="xlsx")


def test_forecast_query_is_bounded():
    assert billing_router.ForecastQuery(days=30).days == 30
    with pytest.raises(ValueError):
        billing_router.ForecastQuery(days=0)
    with pytest.raises(ValueError):
        billing_router.ForecastQuery(days=91)


class Result:
    def __init__(self, row=None, rows=None):
        self.row = row
        self.rows = rows or []

    async def fetchone(self):
        return self.row

    async def fetchall(self):
        return self.rows


class Connection:
    def __init__(self, results=None):
        self.results = results or {}
        self.statements = []

    async def execute(self, statement, params=None):
        self.statements.append((statement, params))
        key = None
        for candidate in ("bill_transaction", "bill_usage_record", "bill_account", "bill_usage_export"):
            if candidate in statement:
                key = candidate
        return Result(self.results.get(key))

    async def commit(self):
        return

    async def __aenter__(self):
        return self

    async def __aexit__(self, exc_type, exc, tb):
        return False


class Pool:
    def __init__(self, connection):
        self.connection_instance = connection

    def connection(self):
        return self.connection_instance


@pytest.mark.asyncio
async def test_get_billing_transaction_returns_row_or_404(monkeypatch):
    actor = owner()
    row = {
        "id": "txn_1",
        "kind": "usage",
        "amount": "-1.23",
        "balance_after": "10.00",
        "reference_id": "ref_1",
        "description": "usage charge",
        "created_at": datetime.now(UTC),
    }
    conn = Connection({"bill_transaction": row})
    monkeypatch.setattr(billing_router.pool, "connection", lambda: conn)
    result = await billing_router.get_billing_transaction("txn_1", actor)
    assert result["id"] == "txn_1"

    conn_missing = Connection({"bill_transaction": None})
    monkeypatch.setattr(billing_router.pool, "connection", lambda: conn_missing)
    with pytest.raises(HTTPException) as error:
        await billing_router.get_billing_transaction("txn_missing", actor)
    assert error.value.status_code == 404


@pytest.mark.asyncio
async def test_create_billing_usage_export_queues_and_returns_accepted(monkeypatch):
    actor = owner()
    now = datetime.now(UTC)
    row = {
        "id": "buex_1",
        "status": "queued",
        "format": "jsonl",
        "record_count": 0,
        "content_hash": "",
        "manifest": {"provider_execution": "pending_external"},
        "created_at": now,
        "expires_at": now,
    }
    conn = Connection({"bill_usage_export": row})
    monkeypatch.setattr(billing_router.pool, "connection", lambda: conn)
    body = billing_router.UsageExportRequest(format="jsonl")
    result = await billing_router.create_billing_usage_export(body, actor)
    assert result["operation_id"].startswith("buex_")
    assert result["id"] == "buex_1"
    assert result["status"] == "accepted"
    assert result["execution_mode"] == "controlled_mock"
    assert any("bill_usage_export" in statement for statement, _ in conn.statements)


@pytest.mark.asyncio
async def test_get_billing_forecast_uses_history_or_mock_fallback(monkeypatch):
    actor = owner()
    history_row = {
        "total_credits": 150.0,
        "total_tokens": 1000,
        "requests": 50,
    }
    account_row = {"available_balance": 75.0}
    conn = Connection({"bill_usage_record": history_row, "bill_account": account_row})
    monkeypatch.setattr(billing_router.pool, "connection", lambda: conn)
    result = await billing_router.get_billing_forecast(actor, days=30)
    assert result["workspace_id"] == actor.workspace_id
    assert result["forecast_days"] == 30
    assert result["recent_credits"] == 150.0
    assert result["daily_average_credits"] == 5.0
    assert result["projected_monthly_credits"] == 150.0
    assert result["available_balance"] == 75.0

    # No history falls back to deterministic mock values.
    conn_empty = Connection({"bill_usage_record": {"total_credits": 0, "total_tokens": 0, "requests": 0}, "bill_account": account_row})
    monkeypatch.setattr(billing_router.pool, "connection", lambda: conn_empty)
    fallback = await billing_router.get_billing_forecast(actor)
    assert fallback["recent_credits"] == 0.0
    assert fallback["projected_monthly_credits"] > 0
    assert fallback["provider_execution"] == "local"
