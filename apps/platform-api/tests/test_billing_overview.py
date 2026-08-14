from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal

import httpx
import pytest
from fastapi import FastAPI

from workama_platform.core import Actor, get_actor
from workama_platform.modules.billing.router import router


class _Result:
    def __init__(self, row=None, rows=None):
        self._row = row
        self._rows = rows if rows is not None else []

    async def fetchone(self):
        return self._row

    async def fetchall(self):
        return list(self._rows)


class _RecordingConnection:
    def __init__(self, results):
        self.calls = []
        self._results = results
        self._idx = 0

    async def execute(self, query, params=()):
        self.calls.append((query, params))
        result = self._results[self._idx] if self._idx < len(self._results) else _Result()
        self._idx += 1
        return result


class _Pool:
    def __init__(self, conn):
        self._conn = conn

    def connection(self):
        outer = self

        class _Ctx:
            async def __aenter__(self):
                return outer._conn

            async def __aexit__(self, *_args):
                return False

        return _Ctx()


@pytest.fixture
def billing_module(monkeypatch):
    from workama_platform.modules.billing import router as billing_router
    return billing_router


def _actor() -> Actor:
    return Actor(
        user_id="usr_test",
        workspace_id="wsp_test",
        org_id="org_test",
        role="owner",
        email="owner@example.com",
        display_name="Owner",
        onboarding_completed=True,
        capabilities=("*",),
    )


@pytest.mark.asyncio
async def test_billing_overview_aggregates_plans_subscription_usage_and_events(monkeypatch, billing_module):
    now = datetime.now(UTC)
    plans = [
        {
            "id": "free",
            "name": "Free",
            "price": Decimal("0.00"),
            "currency": "CNY",
            "quotas": {"members": 1, "gateway_tokens": 5, "agent_concurrency": 1, "granted_credits_month": 500},
        },
        {
            "id": "pro",
            "name": "Pro",
            "price": Decimal("99.00"),
            "currency": "CNY",
            "quotas": {"members": 1, "gateway_tokens": 50, "agent_concurrency": 3, "granted_credits_month": 12000},
        },
    ]
    subscription = {
        "id": "sub_1",
        "plan_code": "pro",
        "plan_name": "Pro",
        "status": "active",
        "started_at": now,
        "renew_at": now,
        "quotas": {"members": 1},
    }
    usage = {
        "requests": 12,
        "prompt_tokens": 1000,
        "completion_tokens": 500,
        "credits_used": Decimal("0.125"),
    }
    events = [
        {"id": "txn_1", "type": "usage", "amount": Decimal("-0.1"), "description": "model usage", "created_at": now},
    ]
    conn = _RecordingConnection([
        _Result(rows=plans),
        _Result(row=subscription),
        _Result(row=usage),
        _Result(rows=events),
    ])
    monkeypatch.setattr(billing_module, "pool", _Pool(conn))

    app = FastAPI()
    app.include_router(router)
    app.dependency_overrides[get_actor] = _actor

    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
        response = await client.get("/api/v1/billing/overview")

    assert response.status_code == 200
    body = response.json()
    assert [plan["id"] for plan in body["plans"]] == ["free", "pro"]
    assert body["plans"][1]["monthly_credits"] == 12000
    assert body["subscription"] == {
        "id": "sub_1",
        "plan_id": "pro",
        "plan_code": "pro",
        "plan_name": "Pro",
        "status": "active",
        "seats": 1,
        "started_at": subscription["started_at"].isoformat(),
        "renew_at": subscription["renew_at"].isoformat(),
    }
    assert body["usage"]["requests"] == 12
    assert body["usage"]["tokens"] == 1500
    assert body["usage"]["credits_used"] == 0.125
    assert body["events"][0]["id"] == "txn_1"
    assert body["events"][0]["currency"] == "credits"
    assert len(conn.calls) == 4
    assert all(not params or params[0] == "wsp_test" for _, params in conn.calls)
