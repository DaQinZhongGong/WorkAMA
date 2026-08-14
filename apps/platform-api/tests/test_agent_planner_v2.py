from __future__ import annotations

import pytest
from fastapi import HTTPException

from workama_platform.modules import agent_planner


class _Result:
    def __init__(self, row=None, rows=None):
        self._row = row
        self._rows = rows if rows is not None else []

    async def fetchone(self):
        return self._row

    async def fetchall(self):
        return self._rows


class _SeqConnection:
    def __init__(self, results=None):
        self._results = list(results or [])
        self.calls = []

    async def execute(self, query, params=()):
        self.calls.append((query, params))
        if self._results:
            return self._results.pop(0)
        return _Result()

    async def commit(self):
        return None

    async def rollback(self):
        return None

    def transaction(self):
        class _Tx:
            async def __aenter__(self):
                return self
            async def __aexit__(self, *_args):
                return False
        return _Tx()


class _Pool:
    def __init__(self, connection):
        self._connection = connection

    def connection(self):
        connection = self._connection

        class _Context:
            async def __aenter__(self):
                return connection

            async def __aexit__(self, *_args):
                return False

        return _Context()


def _actor():
    from workama_platform.core import Actor, ROLE_CAPABILITIES
    return Actor(
        user_id="usr_test",
        workspace_id="wsp_test",
        org_id="org_test",
        role="admin",
        email="admin@example.test",
        display_name="Admin",
        onboarding_completed=True,
        capabilities=ROLE_CAPABILITIES["admin"],
    )


@pytest.mark.asyncio
async def test_recover_session_state_returns_full_shape(monkeypatch):
    session_row = {
        "id": "planner_1", "workspace_id": "wsp_test", "actor_id": "usr_test",
        "status": "active", "context": {}, "plan": {}, "iterations": 3,
        "budget_used": 10.0, "budget_limit": 100.0,
        "parent_session_id": None, "convergence_score": 0.95, "dedup_hash": "abc123",
        "metadata": {}, "created_at": None, "updated_at": None,
    }
    step_rows = [
        {"id": "s1", "session_id": "planner_1", "step_order": 0, "action": "a", "tool_calls": [], "observations": [], "next_choices": [], "status": "completed", "cost": 1.0, "metadata": {}, "created_at": None},
    ]
    conn = _SeqConnection(results=[_Result(row=session_row), _Result(rows=step_rows)])
    monkeypatch.setattr(agent_planner, "pool", _Pool(conn))

    result = await agent_planner.recover_session_state("planner_1", _actor())
    assert result is not None
    assert result["id"] == "planner_1"
    assert result["convergence_score"] == 0.95
    assert len(result["steps"]) == 1


@pytest.mark.asyncio
async def test_recover_session_state_returns_none_for_missing(monkeypatch):
    conn = _SeqConnection(results=[_Result(row=None)])
    monkeypatch.setattr(agent_planner, "pool", _Pool(conn))
    result = await agent_planner.recover_session_state("missing", _actor())
    assert result is None


@pytest.mark.asyncio
async def test_bridge_child_results_summarizes_steps(monkeypatch):
    child_session = {
        "id": "child_1", "workspace_id": "wsp_test", "status": "completed",
        "context": {}, "plan": {}, "iterations": 2, "budget_used": 5.0, "budget_limit": 50.0,
        "parent_session_id": "parent_1", "convergence_score": None, "dedup_hash": None,
        "metadata": {},
    }
    child_steps = [
        {"id": "cs1", "action": "tool_a", "tool_calls": [], "observations": [], "next_choices": [], "status": "completed", "cost": 2.0, "metadata": {}},
        {"id": "cs2", "action": "tool_b", "tool_calls": [], "observations": [], "next_choices": [], "status": "completed", "cost": 3.0, "metadata": {}},
    ]
    parent_meta = {"metadata": {"child_sessions": []}}
    conn = _SeqConnection(results=[
        _Result(row=child_session),
        _Result(rows=child_steps),
        _Result(row={"next_order": 5}),
        _Result(),
        _Result(row=parent_meta),
        _Result(),
    ])
    monkeypatch.setattr(agent_planner, "pool", _Pool(conn))
    result = await agent_planner.bridge_child_results("parent_1", "child_1", _actor())
    assert result["summarized_steps"] == 2
    assert result["parent_session_id"] == "parent_1"


@pytest.mark.asyncio
async def test_bridge_child_results_rejects_wrong_parent(monkeypatch):
    child_session = {
        "id": "child_1", "workspace_id": "wsp_test", "status": "completed",
        "context": {}, "plan": {}, "iterations": 0, "budget_used": 0.0, "budget_limit": 0.0,
        "parent_session_id": "other_parent", "convergence_score": None, "dedup_hash": None,
        "metadata": {},
    }
    conn = _SeqConnection(results=[_Result(row=child_session)])
    monkeypatch.setattr(agent_planner, "pool", _Pool(conn))
    with pytest.raises(HTTPException) as exc:
        await agent_planner.bridge_child_results("parent_1", "child_1", _actor())
    assert exc.value.status_code == 403


@pytest.mark.asyncio
async def test_bridge_child_results_404_for_missing_child(monkeypatch):
    conn = _SeqConnection(results=[_Result(row=None)])
    monkeypatch.setattr(agent_planner, "pool", _Pool(conn))
    with pytest.raises(HTTPException) as exc:
        await agent_planner.bridge_child_results("parent_1", "missing", _actor())
    assert exc.value.status_code == 404


def test_compute_dedup_hash_is_deterministic():
    plan = {"steps": [{"action": "search"}]}
    h1 = agent_planner.compute_dedup_hash(plan)
    h2 = agent_planner.compute_dedup_hash(plan)
    assert h1 == h2
    assert len(h1) == 16


def test_compute_dedup_hash_differs_for_different_plans():
    h1 = agent_planner.compute_dedup_hash({"a": 1})
    h2 = agent_planner.compute_dedup_hash({"a": 2})
    assert h1 != h2


def test_check_convergence_true_when_similar():
    steps = [{"action": "search web"}, {"action": "search web"}]
    result = agent_planner.check_convergence(steps, threshold=0.9)
    assert result["converged"] is True
    assert result["score"] == 1.0


def test_check_convergence_false_when_different():
    steps = [{"action": "search web"}, {"action": "write code"}]
    result = agent_planner.check_convergence(steps, threshold=0.9)
    assert result["converged"] is False
    assert result["score"] < 0.9


def test_check_convergence_insufficient_steps():
    result = agent_planner.check_convergence([{"action": "one"}], threshold=0.9)
    assert result["converged"] is False
    assert result["reason"] == "insufficient_steps"


@pytest.mark.asyncio
async def test_fork_session_creates_child(monkeypatch):
    parent = {
        "id": "parent_1", "workspace_id": "wsp_test", "actor_id": "usr_test",
        "status": "active", "context": {"topic": "ai"}, "plan": {"goal": "test"},
        "budget_used": 5.0, "budget_limit": 100.0, "metadata": {},
    }
    child_row = {
        "id": "planner_child", "workspace_id": "wsp_test", "actor_id": "usr_test",
        "status": "active", "context": {"topic": "ai", "sub": "x"}, "plan": {"goal": "test", "sub": "y"},
        "iterations": 0, "budget_used": 0.0, "budget_limit": 50.0,
        "parent_session_id": "parent_1", "convergence_score": None, "dedup_hash": "hashhash",
        "metadata": {"forked_from": "parent_1"}, "created_at": None, "updated_at": None,
    }
    conn = _SeqConnection(results=[_Result(row=parent), _Result(row={"parent_session_id": None}), _Result(row=child_row)])
    monkeypatch.setattr(agent_planner, "pool", _Pool(conn))
    result = await agent_planner.fork_session(
        "parent_1",
        agent_planner.PlannerSessionFork(context={"sub": "x"}, plan={"sub": "y"}, budget_limit=50.0),
        _actor(),
    )
    assert result["parent_session_id"] == "parent_1"
    assert result["budget_limit"] == 50.0


@pytest.mark.asyncio
async def test_fork_session_inherits_parent_budget_when_zero(monkeypatch):
    parent = {
        "id": "parent_1", "workspace_id": "wsp_test", "actor_id": "usr_test",
        "status": "active", "context": {}, "plan": {},
        "budget_used": 0.0, "budget_limit": 80.0, "metadata": {},
    }
    child_row = {
        "id": "planner_child", "workspace_id": "wsp_test", "actor_id": "usr_test",
        "status": "active", "context": {}, "plan": {},
        "iterations": 0, "budget_used": 0.0, "budget_limit": 80.0,
        "parent_session_id": "parent_1", "convergence_score": None, "dedup_hash": "h",
        "metadata": {"forked_from": "parent_1"}, "created_at": None, "updated_at": None,
    }
    conn = _SeqConnection(results=[_Result(row=parent), _Result(row={"parent_session_id": None}), _Result(row=child_row)])
    monkeypatch.setattr(agent_planner, "pool", _Pool(conn))
    result = await agent_planner.fork_session(
        "parent_1", agent_planner.PlannerSessionFork(budget_limit=0.0), _actor(),
    )
    assert result["budget_limit"] == 80.0


@pytest.mark.asyncio
async def test_fork_session_404_for_missing_parent(monkeypatch):
    conn = _SeqConnection(results=[_Result(row=None)])
    monkeypatch.setattr(agent_planner, "pool", _Pool(conn))
    with pytest.raises(HTTPException) as exc:
        await agent_planner.fork_session("missing", agent_planner.PlannerSessionFork(), _actor())
    assert exc.value.status_code == 404


@pytest.mark.asyncio
async def test_fork_session_409_for_inactive_parent(monkeypatch):
    parent = {
        "id": "parent_1", "workspace_id": "wsp_test", "actor_id": "usr_test",
        "status": "completed", "context": {}, "plan": {},
        "budget_used": 0.0, "budget_limit": 0.0, "metadata": {},
    }
    conn = _SeqConnection(results=[_Result(row=parent)])
    monkeypatch.setattr(agent_planner, "pool", _Pool(conn))
    with pytest.raises(HTTPException) as exc:
        await agent_planner.fork_session("parent_1", agent_planner.PlannerSessionFork(), _actor())
    assert exc.value.status_code == 409


@pytest.mark.asyncio
async def test_converge_session_detects_convergence(monkeypatch):
    session_row = {"id": "planner_1", "workspace_id": "wsp_test", "status": "active"}
    step_rows = [{"action": "search web"}, {"action": "search web"}]
    conn = _SeqConnection(results=[_Result(row=session_row), _Result(rows=step_rows), _Result()])
    monkeypatch.setattr(agent_planner, "pool", _Pool(conn))
    result = await agent_planner.converge_session(
        "planner_1", agent_planner.PlannerConvergeCheck(threshold=0.9), _actor()
    )
    assert result["converged"] is True
    assert result["score"] == 1.0


@pytest.mark.asyncio
async def test_converge_session_no_convergence(monkeypatch):
    session_row = {"id": "planner_1", "workspace_id": "wsp_test", "status": "active"}
    step_rows = [{"action": "search web"}, {"action": "write code"}]
    conn = _SeqConnection(results=[_Result(row=session_row), _Result(rows=step_rows), _Result()])
    monkeypatch.setattr(agent_planner, "pool", _Pool(conn))
    result = await agent_planner.converge_session(
        "planner_1", agent_planner.PlannerConvergeCheck(threshold=0.9), _actor()
    )
    assert result["converged"] is False


@pytest.mark.asyncio
async def test_converge_session_404_for_missing(monkeypatch):
    conn = _SeqConnection(results=[_Result(row=None)])
    monkeypatch.setattr(agent_planner, "pool", _Pool(conn))
    with pytest.raises(HTTPException) as exc:
        await agent_planner.converge_session("missing", agent_planner.PlannerConvergeCheck(), _actor())
    assert exc.value.status_code == 404


def test_agent_planner_router_exposes_v2_contracts():
    paths = {(route.path, tuple(sorted(route.methods or ()))) for route in agent_planner.router.routes}
    assert ("/api/v1/agent/planner/sessions/{session_id}/fork", ("POST",)) in paths
    assert ("/api/v1/agent/planner/sessions/{session_id}/converge", ("POST",)) in paths


@pytest.mark.asyncio
async def test_fork_then_bridge_updates_parent_budget(monkeypatch):
    parent = {
        "id": "parent_1", "workspace_id": "wsp_test", "actor_id": "usr_test",
        "status": "active", "context": {}, "plan": {},
        "budget_used": 0.0, "budget_limit": 100.0, "metadata": {},
    }
    child_row = {
        "id": "planner_child", "workspace_id": "wsp_test", "actor_id": "usr_test",
        "status": "active", "context": {}, "plan": {},
        "iterations": 0, "budget_used": 0.0, "budget_limit": 100.0,
        "parent_session_id": "parent_1", "convergence_score": None, "dedup_hash": "h",
        "metadata": {"forked_from": "parent_1"}, "created_at": None, "updated_at": None,
    }
    conn_fork = _SeqConnection(results=[_Result(row=parent), _Result(row={"parent_session_id": None}), _Result(row=child_row)])
    monkeypatch.setattr(agent_planner, "pool", _Pool(conn_fork))
    forked = await agent_planner.fork_session(
        "parent_1", agent_planner.PlannerSessionFork(), _actor()
    )
    assert forked["parent_session_id"] == "parent_1"

    child_session = {
        "id": "planner_child", "workspace_id": "wsp_test", "status": "completed",
        "context": {}, "plan": {}, "iterations": 1, "budget_used": 7.0, "budget_limit": 100.0,
        "parent_session_id": "parent_1", "convergence_score": None, "dedup_hash": "h",
        "metadata": {},
    }
    child_steps = [
        {"id": "cs1", "action": "tool_a", "tool_calls": [], "observations": [], "next_choices": [], "status": "completed", "cost": 7.0, "metadata": {}},
    ]
    parent_meta = {"metadata": {"child_sessions": []}}
    conn_bridge = _SeqConnection(results=[
        _Result(row=child_session),
        _Result(rows=child_steps),
        _Result(row={"next_order": 0}),
        _Result(),
        _Result(row=parent_meta),
        _Result(),
    ])
    monkeypatch.setattr(agent_planner, "pool", _Pool(conn_bridge))
    bridge_result = await agent_planner.bridge_child_results("parent_1", "planner_child", _actor())
    assert bridge_result["summarized_steps"] == 1


@pytest.mark.asyncio
async def test_agent_planner_v2_schema_includes_new_fields():
    statements = []

    class Connection:
        async def execute(self, statement):
            statements.append(statement)

    await agent_planner.ensure_agent_planner_schema(Connection())
    schema = "\n".join(statements)
    assert "parent_session_id" in schema
    assert "convergence_score" in schema
    assert "dedup_hash" in schema
    assert "idx_ag_planner_session_parent" in schema


@pytest.mark.asyncio
async def test_list_sessions_with_status_filter(monkeypatch):
    rows = [{"id": "planner_1", "budget_used": 0, "budget_limit": 0, "convergence_score": None, "dedup_hash": None}]
    conn = _SeqConnection(results=[_Result(rows=rows), _Result(row={"total": 1})])
    monkeypatch.setattr(agent_planner, "pool", _Pool(conn))
    result = await agent_planner.list_sessions(_actor(), limit=10, offset=0, status="active")
    assert result["items"][0]["id"] == "planner_1"
    query, _ = conn.calls[0]
    assert "status = %s" in query


@pytest.mark.asyncio
async def test_create_session_computes_dedup_hash(monkeypatch):
    row = {
        "id": "planner_1", "workspace_id": "wsp_test", "actor_id": "usr_test",
        "status": "active", "context": {}, "plan": {"goal": "x"}, "iterations": 0,
        "budget_used": 0, "budget_limit": 100, "parent_session_id": None,
        "convergence_score": None, "dedup_hash": "abcd1234abcd1234",
        "metadata": {}, "created_at": None, "updated_at": None,
    }
    conn = _SeqConnection(results=[_Result(row=row)])
    monkeypatch.setattr(agent_planner, "pool", _Pool(conn))
    result = await agent_planner.create_session(agent_planner.PlannerSessionCreate(plan={"goal": "x"}, budget_limit=100), _actor())
    assert result["dedup_hash"] == "abcd1234abcd1234"
    insert_query, _ = conn.calls[0]
    assert "dedup_hash" in insert_query


@pytest.mark.asyncio
async def test_fork_session_enforces_workspace_isolation(monkeypatch):
    parent = {
        "id": "parent_1", "workspace_id": "wsp_other", "actor_id": "usr_test",
        "status": "active", "context": {}, "plan": {},
        "budget_used": 0.0, "budget_limit": 0.0, "metadata": {},
    }
    conn = _SeqConnection(results=[_Result(row=parent)])
    monkeypatch.setattr(agent_planner, "pool", _Pool(conn))
    with pytest.raises(HTTPException) as exc:
        await agent_planner.fork_session("parent_1", agent_planner.PlannerSessionFork(), _actor())
    assert exc.value.status_code == 404


@pytest.mark.asyncio
async def test_converge_session_enforces_workspace_isolation(monkeypatch):
    session_row = {"id": "planner_1", "workspace_id": "wsp_other", "status": "active"}
    conn = _SeqConnection(results=[_Result(row=session_row)])
    monkeypatch.setattr(agent_planner, "pool", _Pool(conn))
    with pytest.raises(HTTPException) as exc:
        await agent_planner.converge_session("planner_1", agent_planner.PlannerConvergeCheck(), _actor())
    assert exc.value.status_code == 404


@pytest.mark.asyncio
async def test_bridge_child_results_with_zero_child_steps(monkeypatch):
    child_session = {
        "id": "child_1", "workspace_id": "wsp_test", "status": "completed",
        "context": {}, "plan": {}, "iterations": 0, "budget_used": 0.0, "budget_limit": 0.0,
        "parent_session_id": "parent_1", "convergence_score": None, "dedup_hash": None,
        "metadata": {},
    }
    parent_meta = {"metadata": {"child_sessions": []}}
    conn = _SeqConnection(results=[
        _Result(row=child_session),
        _Result(rows=[]),
        _Result(row={"next_order": 2}),
        _Result(),
        _Result(row=parent_meta),
        _Result(),
    ])
    monkeypatch.setattr(agent_planner, "pool", _Pool(conn))
    result = await agent_planner.bridge_child_results("parent_1", "child_1", _actor())
    assert result["summarized_steps"] == 0
