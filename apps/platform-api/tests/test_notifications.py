from datetime import UTC, datetime
from decimal import Decimal

import pytest
from fastapi import HTTPException

from workama_platform.core import Actor
from workama_platform.modules.notification import router as notification_router
from workama_platform.modules.notification.router import NotificationPreferenceUpsert
from workama_platform.modules.notification.service import (
    create_automation_run_notification,
    low_balance_dedupe_key,
    should_notify_low_balance,
)


class _Result:
    rowcount = 0

    def __init__(self, row=None, rows=None):
        self._row = row
        self._rows = rows or []

    async def fetchone(self):
        return self._row

    async def fetchall(self):
        return self._rows


class _Connection:
    def __init__(self, result):
        self.result = result
        self.committed = False

    async def execute(self, *_args, **_kwargs):
        return self.result

    async def commit(self):
        self.committed = True


class _ConnectionContext:
    def __init__(self, connection):
        self.connection_value = connection

    async def __aenter__(self):
        return self.connection_value

    async def __aexit__(self, *_args):
        return False


def _actor():
    return Actor(
        user_id="usr_a",
        workspace_id="wsp_a",
        org_id="org_a",
        role="member",
        email="a@example.com",
        display_name="A",
        onboarding_completed=True,
    )


def test_low_balance_threshold_is_strict():
    assert should_notify_low_balance(Decimal("999.999999"), Decimal("1000")) is True
    assert should_notify_low_balance(Decimal("1000"), Decimal("1000")) is False


def test_low_balance_dedupe_key_is_daily_and_workspace_scoped():
    now = datetime(2026, 7, 14, 23, 59, tzinfo=UTC)
    assert low_balance_dedupe_key("wsp_123", now) == "billing.low_balance:wsp_123:2026-07-14"


@pytest.mark.asyncio
async def test_read_receipt_rejects_another_users_workspace_notification(monkeypatch):
    connection = _Connection(_Result())
    monkeypatch.setattr(
        notification_router.pool,
        "connection",
        lambda: _ConnectionContext(connection),
    )

    with pytest.raises(HTTPException) as exc:
        await notification_router.mark_read("ntf_other", _actor())

    assert exc.value.status_code == 404
    assert connection.committed is False


@pytest.mark.asyncio
async def test_forced_billing_in_app_preference_cannot_be_disabled():
    body = NotificationPreferenceUpsert(
        event_type="billing.low_balance", channel="in_app", enabled=False
    )

    with pytest.raises(HTTPException) as exc:
        await notification_router.update_notification_preferences(body, _actor())

    assert exc.value.status_code == 409


@pytest.mark.asyncio
async def test_automation_result_notification_is_terminal_and_idempotent(monkeypatch):
    calls = []

    async def fake_create_notification(_conn, **kwargs):
        calls.append(kwargs)
        return {"id": "ntf_1", "created": True, "dedupe_key": kwargs["dedupe_key"]}

    from workama_platform.modules.notification import service

    monkeypatch.setattr(service, "create_notification", fake_create_notification)
    result = await create_automation_run_notification(
        object(),
        user_id="usr_a",
        workspace_id="wsp_a",
        run_id="autrun_1",
        target_type="workflow",
        target_id="wfl_1",
        status="succeeded",
    )

    assert result["dedupe_key"] == "automation.run:autrun_1"
    assert calls[0]["event_type"] == "automation.run.succeeded"
    assert calls[0]["channels"] == ("in_app", "email")
    with pytest.raises(ValueError):
        await create_automation_run_notification(
            object(), user_id="usr_a", workspace_id="wsp_a", run_id="autrun_1",
            target_type="workflow", target_id="wfl_1", status="running",
        )
