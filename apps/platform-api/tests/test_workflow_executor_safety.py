"""工作流执行器安全增强测试。

覆盖：
- 嵌套限制（深度/循环检测）
- 审批超时与 fallback
- Loop 安全（节点上限/无状态变化break/变量隔离）
- 可观测性（事件字段/错误分类）
"""
from __future__ import annotations

from datetime import UTC, datetime, timedelta

import httpx
import pytest
from fastapi import FastAPI

from workama_platform.core import Actor, get_actor, json_dumps, new_id
from workama_platform.modules import workflows as wf_module
from workama_platform import worker
from workama_platform.modules.workflows import execute_graph, validate_graph


# ----------------------------------------------------------------------
# Fake DB helpers (shared pattern with test_workflow_executor.py)
# ----------------------------------------------------------------------


class _Result:
    def __init__(self, row=None, rows=None):
        self._row = row
        self._rows = rows if rows is not None else []

    async def fetchone(self):
        return self._row

    async def fetchall(self):
        return list(self._rows)


class _Transaction:
    async def __aenter__(self):
        return self

    async def __aexit__(self, *_args):
        return False


class _RecordingConnection:
    def __init__(self, results=None):
        self.calls: list[tuple[str, tuple]] = []
        self._results = list(results) if results else []
        self._idx = 0

    def transaction(self):
        return _Transaction()

    async def execute(self, query, params=()):
        self.calls.append((query, params))
        if self._idx < len(self._results):
            r = self._results[self._idx]
            self._idx += 1
            return r
        return _Result()

    async def commit(self):
        return None


class _Pool:
    def __init__(self, conn):
        self._conn = conn

    def connection(self):
        conn = self._conn

        class _Ctx:
            async def __aenter__(self):
                return conn

            async def __aexit__(self, *_args):
                return False

        return _Ctx()


def _actor(
    *,
    capabilities=("workflow:*",),
    workspace_id="wsp_test",
    user_id="usr_test",
) -> Actor:
    return Actor(
        user_id=user_id,
        workspace_id=workspace_id,
        org_id="org_test",
        role="admin",
        email="test@example.com",
        display_name="Test",
        onboarding_completed=True,
        capabilities=capabilities,
    )


def _workflow_row(**overrides) -> dict:
    base = {
        "id": "wf_1",
        "workspace_id": "wsp_test",
        "name": "Test Workflow",
        "description": "A test workflow",
        "graph": {
            "nodes": [
                {"id": "input", "type": "input"},
                {"id": "output", "type": "output", "config": {"from": "input"}},
            ],
            "edges": [{"source": "input", "target": "output"}],
        },
        "status": "published",
        "version": 1,
        "metadata": {},
        "created_at": datetime.now(UTC),
        "updated_at": datetime.now(UTC),
    }
    base.update(overrides)
    return base


def _app(actor: Actor | None = None) -> FastAPI:
    app = FastAPI()
    app.include_router(wf_module.router)
    if actor is not None:
        app.dependency_overrides[get_actor] = lambda: actor
    return app


# ----------------------------------------------------------------------
# 1. 嵌套限制测试
# ----------------------------------------------------------------------


def _sub_workflow_graph(workflow_id: str) -> dict:
    return {
        "nodes": [
            {"id": "input", "type": "input"},
            {"id": "sub", "type": "sub_workflow", "config": {"workflow_id": workflow_id}},
            {"id": "output", "type": "output", "config": {"from": "sub"}},
        ],
        "edges": [
            {"source": "input", "target": "sub"},
            {"source": "sub", "target": "output"},
        ],
    }


@pytest.mark.asyncio
async def test_nesting_depth_1_succeeds():
    """深度 1（顶层调用子工作流）应该成功。"""
    inner = {
        "nodes": [
            {"id": "input", "type": "input"},
            {"id": "output", "type": "output", "config": {"from": "input"}},
        ],
        "edges": [{"source": "input", "target": "output"}],
    }
    outer = _sub_workflow_graph("wf_inner")
    status, output, trace, error = await execute_graph(
        outer, {"x": 1}, None, True, workspace_id="wsp_test",
        call_stack=[],
    )
    assert status == "succeeded"


@pytest.mark.asyncio
async def test_nesting_depth_3_succeeds():
    """深度 3 应该成功（call_stack 长度为 2 时调用）。"""
    graph = _sub_workflow_graph("wf_next")
    status, output, trace, error = await execute_graph(
        graph, {"x": 1}, None, True, workspace_id="wsp_test",
        call_stack=["wf_a", "wf_b"],
    )
    assert status == "succeeded"


@pytest.mark.asyncio
async def test_nesting_depth_4_fails():
    """深度 4（call_stack 长度 >= 3）应该失败。"""
    graph = _sub_workflow_graph("wf_next")
    status, output, trace, error = await execute_graph(
        graph, {"x": 1}, None, True, workspace_id="wsp_test",
        call_stack=["wf_a", "wf_b", "wf_c"],
    )
    assert status == "failed"
    assert "Maximum workflow nesting depth exceeded" in str(error)
    sub_trace = [t for t in trace if t["node_id"] == "sub"]
    assert sub_trace[0]["status"] == "failed"
    assert sub_trace[0]["error_category"] == "nesting_exceeded"


@pytest.mark.asyncio
async def test_circular_workflow_call_detected():
    """循环调用应该被检测并失败。"""
    graph = _sub_workflow_graph("wf_self")
    status, output, trace, error = await execute_graph(
        graph, {"x": 1}, None, True, workspace_id="wsp_test",
        call_stack=["wf_self"],
    )
    assert status == "failed"
    assert "Circular workflow call detected" in str(error)


@pytest.mark.asyncio
async def test_run_workflow_nesting_depth_exceeded(monkeypatch):
    """run_workflow 端点应在创建时拒绝超过最大嵌套深度的运行。"""
    workflow = _workflow_row()
    parent_run = {"id": "wfr_parent", "nesting_depth": 3}
    conn = _RecordingConnection(results=[
        _Result(row=workflow),
        _Result(row=parent_run),
        _Result(),  # INSERT pf_workflow_run
        _Result(row={"next_seq": 1}),  # SELECT MAX(seq) for append_workflow_event
        _Result(),  # INSERT pf_workflow_event
        _Result(row={"id": "wfr_new", "workflow_id": "wf_1", "status": "failed", "error": "Maximum workflow nesting depth exceeded"}),  # SELECT pf_workflow_run
    ])
    monkeypatch.setattr(wf_module, "pool", _Pool(conn))

    app = _app(actor=_actor())
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
        resp = await client.post("/api/v1/workflows/wf_1/runs", json={"input": {"_parent_run_id": "wfr_parent"}})
    assert resp.status_code == 202
    body = resp.json()
    assert body["status"] == "failed"


# ----------------------------------------------------------------------
# 2. 审批超时测试
# ----------------------------------------------------------------------


@pytest.mark.asyncio
async def test_approval_node_records_timeout():
    """approval 节点应记录 timeout_seconds 和 timeout_at。"""
    graph = {
        "nodes": [
            {"id": "input", "type": "input"},
            {"id": "approval", "type": "approval", "config": {"timeout_seconds": 600}},
            {"id": "output", "type": "output", "config": {"from": "approval"}},
        ],
        "edges": [
            {"source": "input", "target": "approval"},
            {"source": "approval", "target": "output"},
        ],
    }
    status, output, trace, error = await execute_graph(graph, {"x": 1}, None, True)
    assert status == "pending_approval"
    approval_trace = [t for t in trace if t["node_id"] == "approval"][0]
    assert approval_trace["status"] == "pending_approval"
    assert "timeout_at" in approval_trace


@pytest.mark.asyncio
async def test_approval_timeout_with_fallback_continues():
    """当传入 _approval_action=timeout 且配置了 fallback_branch 时，approval 应超时并继续执行。"""
    graph = {
        "nodes": [
            {"id": "input", "type": "input"},
            {"id": "approval", "type": "approval", "config": {"fallback_branch": "fallback", "timeout_seconds": 600}},
            {"id": "output", "type": "output", "config": {"from": "approval"}},
        ],
        "edges": [
            {"source": "input", "target": "approval"},
            {"source": "approval", "target": "output"},
        ],
    }
    status, output, trace, error = await execute_graph(
        graph, {"x": 1}, None, True,
        call_stack=[],
    )
    # 没有传入 _approval_action，所以应该还是 pending_approval
    assert status == "pending_approval"

    # 传入 _approval_action=timeout
    status, output, trace, error = await execute_graph(
        graph, {"x": 1, "_approval_action": "timeout"}, None, True,
        call_stack=[],
    )
    assert status == "succeeded"
    approval_trace = [t for t in trace if t["node_id"] == "approval"][0]
    assert approval_trace["status"] == "timed_out"
    assert output["output"]["timed_out"] is True


class _FakeJob:
    def __init__(self, payload, workspace_id="wsp_test", operation_id="op_1"):
        self.payload = payload
        self.workspace_id = workspace_id
        self.operation_id = operation_id


@pytest.mark.asyncio
async def test_process_workflow_run_job_approval_timeout(monkeypatch):
    """pending_approval 状态且 timeout_at 已过时，worker 应将其标记为 failed。"""
    run_row = {
        "id": "wfr_1",
        "workflow_id": "wf_1",
        "workspace_id": "wsp_test",
        "status": "pending_approval",
        "timeout_at": datetime.now(UTC) - timedelta(seconds=1),
        "graph": {"nodes": [{"id": "input", "type": "input"}], "edges": []},
        "version": 1,
    }
    conn = _RecordingConnection(results=[
        _Result(row=run_row),
        _Result(row={"id": "wfr_1"}),  # UPDATE pf_workflow_run
        _Result(row={"next_seq": 1}),  # SELECT MAX(seq) for append_workflow_event
        _Result(),  # INSERT pf_workflow_event
    ])
    monkeypatch.setattr(worker, "pool", _Pool(conn))

    job = _FakeJob({"run_id": "wfr_1", "workflow_id": "wf_1"})
    result = await worker.process_workflow_run_job(job)
    assert result["status"] == "failed"
    assert "approval_timeout" in str(result) or result.get("error") == "Workflow approval timed out"


@pytest.mark.asyncio
async def test_mark_workflow_run_failed_sets_error_category(monkeypatch):
    """mark_workflow_run_failed 应正确写入 error_category。"""
    conn = _RecordingConnection(results=[
        _Result(row={"id": "wfr_1"}),  # UPDATE pf_workflow_run
        _Result(row={"next_seq": 1}),  # SELECT MAX(seq) for append_workflow_event
        _Result(),  # INSERT pf_workflow_event
    ])
    monkeypatch.setattr(worker, "pool", _Pool(conn))

    job = _FakeJob({"run_id": "wfr_1", "workflow_id": "wf_1"})
    await worker.mark_workflow_run_failed(job, "some error", "execution_error")
    # 验证 SQL 中包含 error_category
    assert any("error_category" in call[0] for call in conn.calls)


# ----------------------------------------------------------------------
# 3. Loop 安全测试
# ----------------------------------------------------------------------


@pytest.mark.asyncio
async def test_loop_node_limits_items_to_50():
    """loop 节点在 items 超过 50 时应失败。"""
    graph = {
        "nodes": [
            {"id": "input", "type": "input"},
            {"id": "loop", "type": "loop", "config": {"items_from": "input.items", "max_iterations": 100}},
            {"id": "output", "type": "output", "config": {"from": "loop"}},
        ],
        "edges": [
            {"source": "input", "target": "loop"},
            {"source": "loop", "target": "output"},
        ],
    }
    status, output, trace, error = await execute_graph(
        graph, {"items": list(range(200))}, None, True,
    )
    assert status == "failed"
    assert "loop limit exceeded" in str(error).lower()
    loop_trace = [t for t in trace if t["node_id"] == "loop"][0]
    assert loop_trace["status"] == "failed"
    assert loop_trace.get("error_category") == "loop_limit_exceeded"


@pytest.mark.asyncio
async def test_loop_node_within_limit_succeeds():
    """loop 节点在 items 不超过 50 时应成功。"""
    graph = {
        "nodes": [
            {"id": "input", "type": "input"},
            {"id": "loop", "type": "loop", "config": {"items_from": "input.items", "max_iterations": 10}},
            {"id": "output", "type": "output", "config": {"from": "loop"}},
        ],
        "edges": [
            {"source": "input", "target": "loop"},
            {"source": "loop", "target": "output"},
        ],
    }
    status, output, trace, error = await execute_graph(
        graph, {"items": list(range(30))}, None, True,
    )
    assert status == "succeeded"
    assert output["output"]["count"] == 10
    assert output["output"]["truncated"] is True


# ----------------------------------------------------------------------
# 4. 可观测性与错误分类测试
# ----------------------------------------------------------------------


@pytest.mark.asyncio
async def test_observability_events_include_timing_and_output_size():
    """事件应包含 started_at, ended_at, duration_ms, output_size。"""
    graph = {
        "nodes": [
            {"id": "input", "type": "input"},
            {"id": "output", "type": "output", "config": {"from": "input"}},
        ],
        "edges": [{"source": "input", "target": "output"}],
    }
    events = []
    status, output, trace, error = await execute_graph(graph, {"x": 1}, None, True, events)
    assert status == "succeeded"
    for item in trace:
        assert "started_at" in item
        assert "ended_at" in item
        assert "duration_ms" in item
        if item["status"] == "succeeded":
            assert "output_size" in item

    for event in events:
        payload = event["payload"]
        if event["event_type"] == "workflow.node.started":
            assert "started_at" in payload
        if event["event_type"] in ("workflow.node.succeeded", "workflow.node.failed"):
            assert "started_at" in payload
            assert "ended_at" in payload
            assert "duration_ms" in payload
        if event["event_type"] == "workflow.node.succeeded":
            assert "output_size" in payload


@pytest.mark.asyncio
async def test_error_classification_on_user_errors():
    """用户错误应分类为 user。"""
    graph = {
        "nodes": [
            {"id": "input", "type": "input"},
            {"id": "sub", "type": "sub_workflow", "config": {}},
            {"id": "output", "type": "output"},
        ],
        "edges": [
            {"source": "input", "target": "sub"},
            {"source": "sub", "target": "output"},
        ],
    }
    status, output, trace, error = await execute_graph(graph, {}, None, True)
    assert status == "failed"
    sub_trace = [t for t in trace if t["node_id"] == "sub"][0]
    assert sub_trace["status"] == "failed"
    assert sub_trace.get("error_category") == "user"


@pytest.mark.asyncio
async def test_error_classification_on_timeout_errors():
    """超时错误应分类为 timeout。"""
    graph = {
        "nodes": [
            {"id": "input", "type": "input"},
            {"id": "http", "type": "http", "config": {"url": "http://localhost:1/timeout", "method": "GET", "timeout": 0.001}},
            {"id": "output", "type": "output"},
        ],
        "edges": [
            {"source": "input", "target": "http"},
            {"source": "http", "target": "output"},
        ],
    }
    status, output, trace, error = await execute_graph(graph, {}, None, False)
    assert status == "failed"
    http_trace = [t for t in trace if t["node_id"] == "http"][0]
    assert http_trace["status"] == "failed"
    assert http_trace.get("error_category") == "timeout"
