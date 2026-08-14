from datetime import UTC, datetime

import pytest
from fastapi import HTTPException

from workama_platform.core import Actor
from workama_platform.modules import automation


def test_cron_parser_supports_ranges_steps_and_sunday_alias():
    fields = automation.parse_cron_expression("*/15 9-17 * * 1-5")
    assert 0 in fields[0] and 45 in fields[0]
    assert fields[1] == frozenset(range(9, 18))
    assert 0 not in fields[4] and 5 in fields[4]


def test_next_cron_at_is_timezone_aware_and_uses_day_or_semantics():
    after = datetime(2026, 7, 15, 0, 1, tzinfo=UTC)
    result = automation.next_cron_at("0 9 15 * 1", after, "Asia/Shanghai")
    assert result == datetime(2026, 7, 15, 1, 0, tzinfo=UTC)
    assert automation.next_cron_at("0 0 * * 0", result, "UTC").weekday() == 6


def test_cron_and_timezone_validation_reject_unsafe_shapes():
    for expression in ("* *", "61 * * * *", "*/0 * * * *", "a * * * *"):
        with pytest.raises(ValueError):
            automation.normalize_cron_expression(expression)
    with pytest.raises(ValueError):
        automation.normalize_timezone("Not/AZone")


def test_automation_targets_must_be_internal_workspace_ids():
    assert automation.normalize_automation_target_id("  wfl_123 ") == "wfl_123"
    for target_id in ("https://example.test/workflow", "mock://workflow", "local://workflow"):
        with pytest.raises(ValueError):
            automation.normalize_automation_target_id(target_id)


def test_schedule_models_reject_external_target_urls():
    with pytest.raises(ValueError):
        automation.ScheduleCreate(
            name="external",
            trigger_type="webhook",
            target_type="workflow",
            target_id="https://example.test/workflow",
        )


def test_schedule_view_never_returns_webhook_secret_or_sensitive_payload():
    view = automation.schedule_view(
        {
            "id": "auto_1",
            "workspace_id": "wsp_1",
            "name": "hook",
            "trigger_type": "webhook",
            "cron_expression": None,
            "timezone": "UTC",
            "target_type": "agent",
            "target_id": "agent_1",
            "payload": {"authorization": "Bearer secret", "safe": "yes"},
            "status": "active",
            "enabled": True,
            "version": 1,
        },
        webhook_secret="only-once",
    )
    assert view["webhook_secret"] == "only-once"
    assert view["payload"]["authorization"] == "<redacted>"


def test_router_exposes_crud_trigger_and_webhook_routes():
    paths = {(route.path, tuple(sorted(route.methods or ()))) for route in automation.router.routes}
    webhook_paths = {(route.path, tuple(sorted(route.methods or ()))) for route in automation.webhook_router.routes}
    assert ("/api/v1/automations", ("GET",)) in paths
    assert ("/api/v1/automations/{schedule_id}/trigger", ("POST",)) in paths
    assert ("/api/v1/automation-webhooks/{schedule_id}", ("POST",)) in webhook_paths


@pytest.mark.asyncio
async def test_schema_is_additive_and_contains_idempotent_run_contract():
    statements = []

    class Connection:
        async def execute(self, statement):
            statements.append(statement)

    await automation.ensure_automation_schema(Connection())
    schema = "\n".join(statements)
    assert "ops_automation_schedule" in schema
    assert "UNIQUE(schedule_id, idempotency_key)" in schema


def _owner(workspace_id: str = "wsp_1") -> Actor:
    return Actor(
        user_id="usr_owner",
        workspace_id=workspace_id,
        org_id="org_1",
        role="owner",
        email="owner@example.test",
        display_name="Owner",
        onboarding_completed=True,
        capabilities=("*",),
    )


class _Result:
    def __init__(self, row=None, rows=None):
        self._row = row
        self._rows = rows or []

    async def fetchone(self):
        return self._row

    async def fetchall(self):
        return self._rows


class _Transaction:
    def __init__(self, connection):
        self._connection = connection

    async def __aenter__(self):
        return self._connection

    async def __aexit__(self, exc_type, exc, tb):
        return False


class _ListConnection:
    """Minimal async context manager backing list endpoints."""

    def __init__(self, rows):
        self._rows = rows

    async def __aenter__(self):
        return self

    async def __aexit__(self, exc_type, exc, tb):
        return False

    async def execute(self, statement, params=None):
        return _Result(rows=self._rows)


class _TriggerConnection:
    """Connection mock backing ``trigger_schedule``'s transactional flow."""

    def __init__(self, schedule, run):
        self._schedule = schedule
        self._run = run

    def transaction(self):
        return _Transaction(self)

    async def __aenter__(self):
        return self

    async def __aexit__(self, exc_type, exc, tb):
        return False

    async def execute(self, statement, params=None):
        if "SELECT * FROM ops_automation_schedule" in statement:
            return _Result(row=self._schedule)
        if "SELECT * FROM ops_automation_run" in statement:
            return _Result(row=None)  # no prior run -> fresh insert path
        if "INSERT INTO ops_automation_run" in statement:
            return _Result(row=self._run)
        return _Result()


@pytest.mark.asyncio
async def test_list_schedules_returns_listresponse_envelope(monkeypatch):
    rows = [
        {
            "id": "auto_1",
            "workspace_id": "wsp_1",
            "name": "cron",
            "trigger_type": "cron",
            "cron_expression": "0 9 * * *",
            "timezone": "UTC",
            "target_type": "agent",
            "target_id": "agent_1",
            "payload": {},
            "status": "active",
            "enabled": True,
            "version": 1,
        }
    ]
    monkeypatch.setattr(automation.pool, "connection", lambda: _ListConnection(rows))
    result = await automation.list_schedules(_owner(), limit=50)
    # Contract《720》listAutomations -> ListResponse<ScheduleDTO>
    assert result["data"] == result["items"]
    assert result["data"][0]["id"] == "auto_1"
    assert result["has_more"] is False
    assert result["next_cursor"] is None
    assert "request_id" in result["meta"]


@pytest.mark.asyncio
async def test_list_runs_returns_listresponse_envelope(monkeypatch):
    run_row = {
        "id": "autrun_1",
        "schedule_id": "auto_1",
        "workspace_id": "wsp_1",
        "trigger_source": "manual",
        "idempotency_key": "manual:x",
        "status": "queued",
        "payload": {},
        "operation_id": None,
        "error_code": None,
        "error_message": None,
        "created_at": datetime.now(UTC),
        "completed_at": None,
    }
    schedule = {
        "id": "auto_1",
        "workspace_id": "wsp_1",
        "name": "cron",
        "trigger_type": "cron",
        "cron_expression": "0 9 * * *",
        "timezone": "UTC",
        "target_type": "agent",
        "target_id": "agent_1",
        "payload": {},
        "status": "active",
        "enabled": True,
        "version": 1,
    }

    class RunsConnection(_ListConnection):
        async def execute(self, statement, params=None):
            if "SELECT * FROM ops_automation_schedule" in statement:
                return _Result(row=schedule)
            return _Result(rows=[run_row])

    monkeypatch.setattr(automation.pool, "connection", lambda: RunsConnection([run_row]))
    result = await automation.list_runs("auto_1", _owner(), limit=50)
    # Contract《720》listAutomationRuns -> ListResponse<ScheduleRunDTO>
    assert result["data"] == result["items"]
    assert result["data"][0]["id"] == "autrun_1"
    assert result["has_more"] is False
    assert "request_id" in result["meta"]


@pytest.mark.asyncio
async def test_trigger_schedule_returns_operation_accepted_envelope(monkeypatch):
    schedule = {
        "id": "auto_1",
        "workspace_id": "wsp_1",
        "name": "cron",
        "trigger_type": "cron",
        "cron_expression": "0 9 * * *",
        "timezone": "UTC",
        "target_type": "agent",
        "target_id": "agent_1",
        "payload": {},
        "status": "active",
        "enabled": True,
        "version": 1,
    }
    submitted_at = datetime.now(UTC)
    run = {
        "id": "autrun_1",
        "schedule_id": "auto_1",
        "workspace_id": "wsp_1",
        "trigger_source": "manual",
        "idempotency_key": "manual:x",
        "status": "queued",
        "payload": {},
        "operation_id": None,
        "error_code": None,
        "error_message": None,
        "created_at": submitted_at,
        "completed_at": None,
    }
    monkeypatch.setattr(automation.pool, "connection", lambda: _TriggerConnection(schedule, run))
    result = await automation.trigger_schedule(
        "auto_1", automation.TriggerRequest(payload={}), _owner(), None
    )
    # Contract《720》triggerAutomation -> OperationAccepted
    assert result["operation_id"] == "autrun_1"
    assert result["status"] == "queued"
    assert result["status_url"] == "/api/v1/automations/auto_1/runs"
    assert result["submitted_at"] == submitted_at
    # Backward-compatible fields retained
    assert result["run"]["id"] == "autrun_1"
    assert result["schedule_id"] == "auto_1"
