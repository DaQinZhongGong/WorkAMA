import json
from datetime import UTC, datetime, timedelta

import pytest

from workama_platform import worker


class _Result:
    def __init__(self, rows=None):
        self.rows = list(rows or [])

    async def fetchall(self):
        return self.rows

    async def fetchone(self):
        return self.rows[0] if self.rows else None


class _Transaction:
    def __init__(self, connection):
        self.connection = connection

    async def __aenter__(self):
        return self.connection

    async def __aexit__(self, exc_type, exc, traceback):
        return False


class _Pool:
    def __init__(self, connection):
        self.connection_value = connection

    def connection(self):
        class _Context:
            async def __aenter__(_self):
                return self.connection_value

            async def __aexit__(_self, exc_type, exc, traceback):
                return False

        return _Context()


def _schedule(next_run_at):
    return {
        "id": "auto_schedule_1",
        "workspace_id": "wsp_1",
        "trigger_type": "cron",
        "cron_expression": "* * * * *",
        "timezone": "UTC",
        "target_type": "agent",
        "target_id": "agent_1",
        "payload": {"safe": "value"},
        "status": "active",
        "enabled": True,
        "next_run_at": next_run_at,
        "created_at": next_run_at - timedelta(minutes=1),
    }


class _ScanConnection:
    def __init__(self, schedule, existing=False):
        self.schedule = schedule
        self.existing = existing
        self.statements = []

    def transaction(self):
        return _Transaction(self)

    async def execute(self, statement, params=()):
        normalized = " ".join(statement.split())
        self.statements.append((normalized, params))
        if normalized.startswith("SELECT * FROM ops_automation_schedule"):
            return _Result([self.schedule])
        if normalized.startswith("SELECT id FROM ops_automation_run"):
            return _Result([{"id": "autrun_existing"}] if self.existing else [])
        if normalized.startswith("UPDATE ops_automation_schedule SET next_run_at"):
            return _Result([])
        raise AssertionError(f"unexpected scan SQL: {normalized}")


class _RunConnection:
    def __init__(self):
        self.status = "queued"
        self.outbox = []
        self.statements = []
        self.run = {
            "id": "autrun_1",
            "schedule_id": "auto_schedule_1",
            "workspace_id": "wsp_1",
            "target_type": "work_plan",
            "target_id": "plan_1",
            "status": "queued",
            "payload": {"safe": "value"},
        }

    def transaction(self):
        return _Transaction(self)

    async def execute(self, statement, params=()):
        normalized = " ".join(statement.split())
        self.statements.append((normalized, params))
        if normalized.startswith("SELECT r.*, s.target_type"):
            return _Result([{**self.run, "status": self.status}] if self.status == "queued" else [])
        if normalized.startswith("UPDATE ops_automation_run SET status='running'"):
            if self.status != "queued":
                return _Result([])
            self.status = "running"
            return _Result([{**self.run, "status": self.status}])
        if normalized.startswith("UPDATE ops_automation_run SET status=%s"):
            assert self.status == "running"
            self.status = params[0]
            return _Result([{**self.run, "status": self.status}])
        if normalized.startswith("INSERT INTO ops_outbox"):
            self.outbox.append(
                {
                    "id": params[0],
                    "event_type": params[1],
                    "workspace_id": params[2],
                    "trace_id": params[3],
                    "payload": params[4],
                }
            )
            return _Result([])
        raise AssertionError(f"unexpected run SQL: {normalized}")


@pytest.mark.asyncio
async def test_scan_due_cron_enqueues_one_stable_occurrence(monkeypatch):
    reference = datetime(2026, 7, 16, 2, 3, 45, tzinfo=UTC)
    connection = _ScanConnection(_schedule(reference - timedelta(minutes=1)))
    monkeypatch.setattr(worker, "pool", _Pool(connection))
    calls = []

    async def enqueue(_conn, schedule, **kwargs):
        calls.append((schedule["id"], kwargs))
        return {"id": "autrun_1", "status": "queued"}

    monkeypatch.setattr(worker.automation, "_enqueue_run", enqueue)

    result = await worker.scan_due_automation_schedules(now=reference, limit=10)

    assert result == {"scanned": 1, "enqueued": 1, "deduplicated": 0}
    assert calls[0][1]["source"] == "cron"
    assert calls[0][1]["idempotency_key"] == worker.automation_cron_idempotency_key(
        "auto_schedule_1", reference - timedelta(minutes=1)
    )
    assert "FOR UPDATE SKIP LOCKED" in connection.statements[0][0]


@pytest.mark.asyncio
async def test_scan_due_cron_counts_existing_occurrence_and_advances_schedule(monkeypatch):
    reference = datetime(2026, 7, 16, 2, 3, tzinfo=UTC)
    connection = _ScanConnection(_schedule(reference - timedelta(minutes=1)), existing=True)
    monkeypatch.setattr(worker, "pool", _Pool(connection))

    async def enqueue(_conn, _schedule, **_kwargs):
        return {"id": "autrun_existing", "status": "queued"}

    monkeypatch.setattr(worker.automation, "_enqueue_run", enqueue)

    result = await worker.scan_due_automation_schedules(now=reference)

    assert result == {"scanned": 1, "enqueued": 0, "deduplicated": 1}
    assert any("UPDATE ops_automation_schedule SET next_run_at" in item[0] for item in connection.statements)


@pytest.mark.asyncio
async def test_queued_automation_run_is_terminally_unsupported_without_fake_execution(monkeypatch):
    connection = _RunConnection()
    monkeypatch.setattr(worker, "pool", _Pool(connection))

    first = await worker.process_automation_runs("worker-test", limit=10)
    second = await worker.process_automation_runs("worker-test", limit=10)

    assert first == {"claimed": 1, "succeeded": 0, "failed": 1, "unsupported": 1, "pending": 0}
    assert second == {"claimed": 0, "succeeded": 0, "failed": 0, "unsupported": 0, "pending": 0}
    assert connection.status == "failed"
    assert len(connection.outbox) == 1
    assert connection.outbox[0]["id"] == "out_autrun_1_result"
    payload = json.loads(connection.outbox[0]["payload"])
    assert payload["status"] == "failed"
    assert payload["execution_status"] == "unsupported"
    assert payload["executed"] is False
    assert payload["error_code"] == "unsupported_target"
    assert "no work, workflow, or agent action was executed" in payload["error_message"]
    assert any("ON CONFLICT(id) DO NOTHING" in item[0] for item in connection.statements)


@pytest.mark.parametrize("target_type", ["work_plan", "workflow", "agent"])
def test_unsupported_result_is_explicit_for_every_automation_target(target_type):
    result = worker._unsupported_automation_result({"target_type": target_type})

    assert result["execution_status"] == "unsupported"
    assert result["executed"] is False
    assert result["status"] == "failed"


@pytest.mark.asyncio
async def test_internal_workflow_and_agent_targets_use_real_executor_boundaries(monkeypatch):
    calls = []

    async def workflow_executor(run):
        calls.append(("workflow", run["target_id"]))
        return {"status": "succeeded", "execution_status": "succeeded", "executed": True}

    async def agent_executor(run):
        calls.append(("agent", run["target_id"]))
        return {"status": "succeeded", "execution_status": "succeeded", "executed": True}

    monkeypatch.setattr(worker, "_execute_workflow_target", workflow_executor)
    monkeypatch.setattr(worker, "_execute_agent_target", agent_executor)

    assert await worker._execute_automation_target({"target_type": "workflow", "target_id": "wfl_1"}) == {
        "status": "succeeded", "execution_status": "succeeded", "executed": True
    }
    assert await worker._execute_automation_target({"target_type": "agent", "target_id": "sess_1"}) == {
        "status": "succeeded", "execution_status": "succeeded", "executed": True
    }
    assert calls == [("workflow", "wfl_1"), ("agent", "sess_1")]


@pytest.mark.asyncio
async def test_terminal_automation_result_emits_one_notification_with_result_outbox(monkeypatch):
    connection = _RunConnection()
    connection.run["created_by"] = "usr_creator"
    monkeypatch.setattr(worker, "pool", _Pool(connection))
    notifications = []

    async def execute(_run):
        return {"status": "succeeded", "execution_status": "succeeded", "executed": True}

    async def notify(_conn, **kwargs):
        notifications.append(kwargs)
        return {"id": "ntf_1", "created": True}

    monkeypatch.setattr(worker, "_execute_automation_target", execute)
    monkeypatch.setattr(worker, "create_automation_run_notification", notify)

    result = await worker.process_automation_runs("worker-test", limit=10)

    assert result == {"claimed": 1, "succeeded": 1, "failed": 0, "unsupported": 0, "pending": 0}
    assert len(connection.outbox) == 1
    assert notifications[0]["run_id"] == "autrun_1"
    assert notifications[0]["status"] == "succeeded"


def test_workflow_executor_allows_only_deterministic_node_types():
    assert "llm" not in worker.AUTOMATION_DETERMINISTIC_WORKFLOW_NODES
    assert "transform" in worker.AUTOMATION_DETERMINISTIC_WORKFLOW_NODES
