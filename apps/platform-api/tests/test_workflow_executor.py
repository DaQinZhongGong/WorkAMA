from __future__ import annotations

from datetime import UTC, datetime

import httpx
import pytest
from fastapi import FastAPI

from workama_platform.core import Actor, get_actor
from workama_platform.modules import workflow as wf


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
        "nodes": [
            {"id": "n1", "type": "output", "name": "out", "config": {"fields": ["answer"]}},
        ],
        "edges": [],
        "status": "published",
        "version": 1,
        "metadata": {},
        "created_at": datetime.now(UTC),
        "updated_at": datetime.now(UTC),
    }
    base.update(overrides)
    return base


def _run_row(**overrides) -> dict:
    base = {
        "id": "wfr_1",
        "workflow_id": "wf_1",
        "workspace_id": "wsp_test",
        "input": {"query": "Hello"},
        "output": {"answer": "Hi there"},
        "status": "completed",
        "started_at": datetime.now(UTC),
        "completed_at": datetime.now(UTC),
        "error": None,
        "metadata": {"node_runs": []},
        "created_at": datetime.now(UTC),
    }
    base.update(overrides)
    return base


def _app(actor: Actor | None = None) -> FastAPI:
    app = FastAPI()
    app.include_router(wf.router)
    if actor is not None:
        app.dependency_overrides[get_actor] = lambda: actor
    return app


@pytest.mark.asyncio
async def test_execute_sub_workflow_node_mock():
    node = {
        "id": "n1",
        "type": "sub_workflow",
        "config": {"workflow_id": "wf_sub", "input_mapping": {"q": "query"}},
    }
    output = await wf._execute_sub_workflow(node, {"query": "hello"}, workspace_id="wsp_test", actor=_actor())
    assert output["sub_workflow_id"] == "wf_sub"
    assert output["input"] == {"q": "hello"}
    assert "mock-sub-workflow" in output["output"]["result"]


@pytest.mark.asyncio
async def test_execute_sub_workflow_node_requires_workflow_id():
    node = {"id": "n1", "type": "sub_workflow", "config": {}}
    output = await wf._execute_sub_workflow(node, {}, workspace_id="wsp_test", actor=_actor())
    assert "error" in output


@pytest.mark.asyncio
async def test_execute_loop_node_default_iterations():
    node = {"id": "n1", "type": "loop", "config": {}}
    output = await wf._execute_loop(node, {}, workspace_id="wsp_test", actor=_actor())
    assert output["iterations"] == 10
    assert output["max_iterations"] == 10


@pytest.mark.asyncio
async def test_execute_loop_node_respects_break_condition():
    node = {"id": "n1", "type": "loop", "config": {"break_condition": "done"}}
    output = await wf._execute_loop(node, {"done": True}, workspace_id="wsp_test", actor=_actor())
    assert output["iterations"] == 1


@pytest.mark.asyncio
async def test_execute_loop_node_caps_at_100():
    node = {"id": "n1", "type": "loop", "config": {"max_iterations": 200}}
    output = await wf._execute_loop(node, {}, workspace_id="wsp_test", actor=_actor())
    assert output["max_iterations"] == 100
    assert output["iterations"] == 100


@pytest.mark.asyncio
async def test_execute_loop_node_custom_iterations():
    node = {"id": "n1", "type": "loop", "config": {"max_iterations": 5}}
    output = await wf._execute_loop(node, {}, workspace_id="wsp_test", actor=_actor())
    assert output["iterations"] == 5
    assert len(output["outputs"]) == 5


@pytest.mark.asyncio
async def test_execute_human_approval_node_mock(monkeypatch):
    node = {"id": "n1", "type": "human_approval", "config": {"tool_name": "deploy", "preview": {"env": "prod"}}}

    class _FakeConn:
        async def execute(self, q, p):
            class _R:
                async def fetchone(self):
                    return {"id": "apr_1"}
            return _R()
        async def commit(self):
            return None

    class _FakePool:
        def connection(self):
            class _Ctx:
                async def __aenter__(self):
                    return _FakeConn()
                async def __aexit__(self, *_args):
                    return False
            return _Ctx()

    monkeypatch.setattr(wf, "pool", _FakePool())
    output = await wf._execute_human_approval(node, {}, workspace_id="wsp_test", actor=_actor())
    assert output["approval_id"].startswith("apr_")
    assert output["tool_name"] == "deploy"
    assert output["status"] == "pending"


@pytest.mark.asyncio
async def test_execute_human_approval_node_uses_defaults(monkeypatch):
    node = {"id": "n1", "type": "human_approval", "config": {}}

    class _FakeConn:
        async def execute(self, q, p):
            class _R:
                async def fetchone(self):
                    return {"id": "apr_2"}
            return _R()
        async def commit(self):
            return None

    class _FakePool:
        def connection(self):
            class _Ctx:
                async def __aenter__(self):
                    return _FakeConn()
                async def __aexit__(self, *_args):
                    return False
            return _Ctx()

    monkeypatch.setattr(wf, "pool", _FakePool())
    output = await wf._execute_human_approval(node, {}, workspace_id="wsp_test", actor=_actor())
    assert output["tool_name"] == "workflow_approval"


def test_execute_start_node():
    node = {"id": "n1", "type": "start"}
    output = wf._execute_start(node, {"query": "hi"})
    assert output["input"]["query"] == "hi"


def test_execute_end_node():
    node = {"id": "n1", "type": "end"}
    output = wf._execute_end(node, {"answer": "42"})
    assert output["output"]["answer"] == "42"


@pytest.mark.asyncio
async def test_execute_node_with_retry_succeeds_first_try():
    node = {"id": "n1", "type": "output", "config": {"fields": ["a"]}}
    output, branch = await wf._execute_node_with_retry(node, {"a": 1}, workspace_id="wsp_test", actor=_actor(), max_retries=3)
    assert "error" not in output
    assert branch is None


@pytest.mark.asyncio
async def test_execute_node_with_retry_records_retries(monkeypatch):
    node = {"id": "n1", "type": "llm_call", "config": {"model": "gpt-4o", "prompt_template": "Hi"}}
    monkeypatch.setenv("WORKAMA_INTERNAL_LLM_API_KEY", "")
    output, branch = await wf._execute_node_with_retry(node, {}, workspace_id="wsp_test", actor=_actor(), max_retries=0)
    assert "error" not in output
    assert "mock-llm" in output.get("message", "")


@pytest.mark.asyncio
async def test_execute_workflow_skips_downstream_on_failure():
    workflow = {
        "nodes": [
            {"id": "n1", "type": "tool_call", "config": {"tool_id": "fail_tool"}},
            {"id": "n2", "type": "output", "config": {"fields": ["result"]}},
        ],
        "edges": [{"source": "n1", "target": "n2"}],
    }

    async def _failing_tool_call(node, inputs):
        raise RuntimeError("tool error")

    import workama_platform.modules.workflow as _wf
    original = _wf._execute_tool_call
    _wf._execute_tool_call = _failing_tool_call
    try:
        final_output, node_runs = await wf._execute_workflow(workflow, {"result": "x"}, workspace_id="wsp_test", actor=_actor())
        n2_run = [nr for nr in node_runs if nr["node_id"] == "n2"][0]
        assert n2_run["status"] == "skipped"
    finally:
        _wf._execute_tool_call = original


@pytest.mark.asyncio
async def test_execute_workflow_continue_on_error_allows_downstream():
    workflow = {
        "nodes": [
            {"id": "n1", "type": "tool_call", "config": {"tool_id": "fail_tool"}},
            {"id": "n2", "type": "output", "config": {"fields": ["result"], "continue_on_error": True}},
        ],
        "edges": [{"source": "n1", "target": "n2"}],
    }

    async def _failing_tool_call(node, inputs):
        raise RuntimeError("tool error")

    import workama_platform.modules.workflow as _wf
    original = _wf._execute_tool_call
    _wf._execute_tool_call = _failing_tool_call
    try:
        final_output, node_runs = await wf._execute_workflow(workflow, {"result": "x"}, workspace_id="wsp_test", actor=_actor())
        n2_run = [nr for nr in node_runs if nr["node_id"] == "n2"][0]
        assert n2_run["status"] == "completed"
    finally:
        _wf._execute_tool_call = original


@pytest.mark.asyncio
async def test_execute_workflow_records_error_in_node_run():
    workflow = {
        "nodes": [
            {"id": "n1", "type": "sub_workflow", "config": {}},
        ],
        "edges": [],
    }
    final_output, node_runs = await wf._execute_workflow(workflow, {}, workspace_id="wsp_test", actor=_actor())
    assert node_runs[0]["status"] == "failed"
    assert "error" in node_runs[0]["output"]


@pytest.mark.asyncio
async def test_execute_workflow_retry_on_transient_failure(monkeypatch):
    workflow = {
        "nodes": [
            {"id": "n1", "type": "tool_call", "config": {"tool_id": "x"}},
        ],
        "edges": [],
    }
    call_count = 0

    async def _flaky_tool_call(node, inputs):
        nonlocal call_count
        call_count += 1
        if call_count < 2:
            raise RuntimeError("transient")
        return {"tool_id": "x", "result": "ok", "method": "mock"}

    import workama_platform.modules.workflow as _wf
    original = _wf._execute_tool_call
    _wf._execute_tool_call = _flaky_tool_call
    try:
        final_output, node_runs = await wf._execute_workflow(workflow, {}, workspace_id="wsp_test", actor=_actor())
        assert node_runs[0]["status"] == "completed"
        assert node_runs[0]["output"]["result"] == "ok"
    finally:
        _wf._execute_tool_call = original


@pytest.mark.asyncio
async def test_run_workflow_with_sub_workflow_node(monkeypatch):
    workflow = _workflow_row(
        nodes=[
            {"id": "n1", "type": "sub_workflow", "name": "sub", "config": {"workflow_id": "wf_2", "input_mapping": {"q": "query"}}},
            {"id": "n2", "type": "output", "name": "out", "config": {"fields": ["result"]}},
        ],
        edges=[{"source": "n1", "target": "n2"}],
    )
    run_row = _run_row()
    conn = _RecordingConnection(results=[_Result(row=workflow), _Result(row=run_row)])
    monkeypatch.setattr(wf, "pool", _Pool(conn))

    app = _app(actor=_actor())
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
        resp = await client.post("/api/v1/workflows/wf_1/run", json={"input": {"query": "hi"}})
    assert resp.status_code == 200
    body = resp.json()
    assert len(body["metadata"]["node_runs"]) == 2
    assert body["metadata"]["node_runs"][0]["node_type"] == "sub_workflow"


@pytest.mark.asyncio
async def test_run_workflow_with_loop_node(monkeypatch):
    workflow = _workflow_row(
        nodes=[
            {"id": "n1", "type": "loop", "name": "loop", "config": {"max_iterations": 3}},
            {"id": "n2", "type": "output", "name": "out", "config": {"fields": ["iterations"]}},
        ],
        edges=[{"source": "n1", "target": "n2"}],
    )
    run_row = _run_row()
    conn = _RecordingConnection(results=[_Result(row=workflow), _Result(row=run_row)])
    monkeypatch.setattr(wf, "pool", _Pool(conn))

    app = _app(actor=_actor())
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
        resp = await client.post("/api/v1/workflows/wf_1/run", json={"input": {}})
    assert resp.status_code == 200
    body = resp.json()
    assert len(body["metadata"]["node_runs"]) == 2
    assert body["metadata"]["node_runs"][0]["node_type"] == "loop"


@pytest.mark.asyncio
async def test_run_workflow_with_human_approval_node(monkeypatch):
    workflow = _workflow_row(
        nodes=[
            {"id": "n1", "type": "human_approval", "name": "approve", "config": {"tool_name": "deploy"}},
            {"id": "n2", "type": "output", "name": "out", "config": {"fields": ["approval_id"]}},
        ],
        edges=[{"source": "n1", "target": "n2"}],
    )
    run_row = _run_row()

    class _FakeConn:
        async def execute(self, q, p):
            class _R:
                async def fetchone(self):
                    if "workflow_run" in q:
                        return dict(run_row)
                    return {"id": "apr_1"}
            return _R()
        async def commit(self):
            return None
        def transaction(self):
            class _Tx:
                async def __aenter__(self): return self
                async def __aexit__(self, *_args): return False
            return _Tx()

    class _FakePool:
        def connection(self):
            class _Ctx:
                async def __aenter__(self):
                    return _FakeConn()
                async def __aexit__(self, *_args):
                    return False
            return _Ctx()

    monkeypatch.setattr(wf, "pool", _FakePool())

    async def _owned_workflow_fake(conn, workflow_id, actor):
        return workflow
    monkeypatch.setattr(wf, "_owned_workflow", _owned_workflow_fake)

    app = _app(actor=_actor())
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
        resp = await client.post("/api/v1/workflows/wf_1/run", json={"input": {}})
    assert resp.status_code == 200
    body = resp.json()
    assert len(body["metadata"]["node_runs"]) == 2
    assert body["metadata"]["node_runs"][0]["node_type"] == "human_approval"


@pytest.mark.asyncio
async def test_run_workflow_with_start_end_nodes(monkeypatch):
    workflow = _workflow_row(
        nodes=[
            {"id": "n1", "type": "start", "name": "start"},
            {"id": "n2", "type": "end", "name": "end"},
        ],
        edges=[{"source": "n1", "target": "n2"}],
    )
    run_row = _run_row()
    conn = _RecordingConnection(results=[_Result(row=workflow), _Result(row=run_row)])
    monkeypatch.setattr(wf, "pool", _Pool(conn))

    app = _app(actor=_actor())
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
        resp = await client.post("/api/v1/workflows/wf_1/run", json={"input": {"a": 1}})
    assert resp.status_code == 200
    body = resp.json()
    assert len(body["metadata"]["node_runs"]) == 2


@pytest.mark.asyncio
async def test_create_workflow_with_sub_workflow_type(monkeypatch):
    row = _workflow_row()
    conn = _RecordingConnection(results=[_Result(row=row)])
    monkeypatch.setattr(wf, "pool", _Pool(conn))

    app = _app(actor=_actor())
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
        resp = await client.post("/api/v1/workflows", json={
            "name": "Wf",
            "nodes": [
                {"id": "n1", "type": "sub_workflow", "config": {"workflow_id": "wf_2"}},
                {"id": "n2", "type": "end"},
            ],
            "edges": [{"source": "n1", "target": "n2"}],
        })
    assert resp.status_code == 201


@pytest.mark.asyncio
async def test_create_workflow_with_loop_type(monkeypatch):
    row = _workflow_row()
    conn = _RecordingConnection(results=[_Result(row=row)])
    monkeypatch.setattr(wf, "pool", _Pool(conn))

    app = _app(actor=_actor())
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
        resp = await client.post("/api/v1/workflows", json={
            "name": "Wf",
            "nodes": [
                {"id": "n1", "type": "loop", "config": {"max_iterations": 5}},
                {"id": "n2", "type": "end"},
            ],
            "edges": [{"source": "n1", "target": "n2"}],
        })
    assert resp.status_code == 201


@pytest.mark.asyncio
async def test_create_workflow_with_human_approval_type(monkeypatch):
    row = _workflow_row()
    conn = _RecordingConnection(results=[_Result(row=row)])
    monkeypatch.setattr(wf, "pool", _Pool(conn))

    app = _app(actor=_actor())
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
        resp = await client.post("/api/v1/workflows", json={
            "name": "Wf",
            "nodes": [
                {"id": "n1", "type": "human_approval", "config": {"tool_name": "deploy"}},
                {"id": "n2", "type": "end"},
            ],
            "edges": [{"source": "n1", "target": "n2"}],
        })
    assert resp.status_code == 201


@pytest.mark.asyncio
async def test_node_run_includes_error_field():
    workflow = {
        "nodes": [
            {"id": "n1", "type": "sub_workflow", "config": {}},
        ],
        "edges": [],
    }
    final_output, node_runs = await wf._execute_workflow(workflow, {}, workspace_id="wsp_test", actor=_actor())
    assert node_runs[0].get("error") is not None


@pytest.mark.asyncio
async def test_node_run_includes_retries_on_failure(monkeypatch):
    workflow = {
        "nodes": [
            {"id": "n1", "type": "tool_call", "config": {"tool_id": "x"}},
        ],
        "edges": [],
    }
    call_count = 0

    async def _always_failing(node, inputs):
        nonlocal call_count
        call_count += 1
        raise RuntimeError("fail")

    import workama_platform.modules.workflow as _wf
    original = _wf._execute_tool_call
    _wf._execute_tool_call = _always_failing
    try:
        final_output, node_runs = await wf._execute_workflow(workflow, {}, workspace_id="wsp_test", actor=_actor())
        assert node_runs[0]["status"] == "failed"
        assert node_runs[0]["output"].get("retries") == 3
    finally:
        _wf._execute_tool_call = original


@pytest.mark.asyncio
async def test_e2e_failure_with_continue_on_error(monkeypatch):
    workflow = _workflow_row(
        nodes=[
            {"id": "n1", "type": "sub_workflow", "config": {}},
            {"id": "n2", "type": "output", "config": {"fields": ["a"], "continue_on_error": True}},
        ],
        edges=[{"source": "n1", "target": "n2"}],
    )
    run_row = _run_row()
    conn = _RecordingConnection(results=[_Result(row=workflow), _Result(row=run_row)])
    monkeypatch.setattr(wf, "pool", _Pool(conn))

    app = _app(actor=_actor())
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
        resp = await client.post("/api/v1/workflows/wf_1/run", json={"input": {"a": 1}})
    assert resp.status_code == 200
    body = resp.json()
    n1 = [nr for nr in body["metadata"]["node_runs"] if nr["node_id"] == "n1"][0]
    n2 = [nr for nr in body["metadata"]["node_runs"] if nr["node_id"] == "n2"][0]
    assert n1["status"] == "failed"
    assert n2["status"] == "completed"
