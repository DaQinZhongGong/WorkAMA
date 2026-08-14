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


# --- Coordinator logic tests -------------------------------------------


def test_build_dependency_graph_extracts_observation_links():
    steps = [
        {"id": "s1", "observations": [{"depends_on": "s0"}]},
        {"id": "s2", "observations": [{"depends_on": "s1"}]},
        {"id": "s3", "observations": []},
    ]
    graph = agent_planner.build_dependency_graph(steps)
    assert graph["s1"] == {"s0"}
    assert graph["s2"] == {"s1"}
    assert graph["s3"] == set()


def test_detect_cycle_finds_circular_dependencies():
    cyclic = {"a": {"b"}, "b": {"c"}, "c": {"a"}}
    assert agent_planner.detect_cycle(cyclic) is True


def test_detect_cycle_returns_false_for_dag():
    dag = {"a": set(), "b": {"a"}, "c": {"b"}}
    assert agent_planner.detect_cycle(dag) is False


def test_track_budget_under_limit():
    result = agent_planner.track_budget(100.0, [{"cost": 30.0}, {"cost": 20.0}])
    assert result["budget_used"] == 50.0
    assert result["remaining"] == 50.0
    assert result["status"] == "under"


def test_track_budget_at_limit():
    result = agent_planner.track_budget(50.0, [{"cost": 50.0}])
    assert result["status"] == "at_limit"
    assert result["remaining"] == 0.0


def test_track_budget_over_limit():
    result = agent_planner.track_budget(40.0, [{"cost": 50.0}])
    assert result["status"] == "over"
    assert result["remaining"] == 0.0


def test_semantic_dedup_removes_near_duplicates():
    actions = ["search web", "search web", "write code", "search  web"]
    unique = agent_planner.semantic_dedup(actions, threshold=0.85)
    assert len(unique) == 2


def test_semantic_dedup_returns_empty_for_empty_input():
    assert agent_planner.semantic_dedup([]) == []


def test_levenshtein_distance_is_symmetric():
    assert agent_planner._levenshtein_distance("kitten", "sitting") == 3
    assert agent_planner._levenshtein_distance("sitting", "kitten") == 3
    assert agent_planner._levenshtein_distance("", "abc") == 3
    assert agent_planner._levenshtein_distance("abc", "abc") == 0


# --- Schema test -------------------------------------------------------


@pytest.mark.asyncio
async def test_agent_planner_schema_includes_session_and_step():
    statements = []

    class Connection:
        async def execute(self, statement):
            statements.append(statement)

    await agent_planner.ensure_agent_planner_schema(Connection())
    schema = "\n".join(statements)
    assert "ag_planner_session" in schema
    assert "ag_planner_step" in schema
    assert "budget_used" in schema
    assert "step_order" in schema
    assert "uq_ag_planner_step_session_order" in schema


# --- Router contract tests ---------------------------------------------


def test_agent_planner_router_exposes_all_contracts():
    paths = {(route.path, tuple(sorted(route.methods or ()))) for route in agent_planner.router.routes}
    assert ("/api/v1/agent/planner/sessions", ("GET",)) in paths
    assert ("/api/v1/agent/planner/sessions", ("POST",)) in paths
    assert ("/api/v1/agent/planner/sessions/{session_id}", ("DELETE",)) in paths
    assert ("/api/v1/agent/planner/sessions/{session_id}", ("GET",)) in paths
    assert ("/api/v1/agent/planner/sessions/{session_id}/steps", ("GET",)) in paths
    assert ("/api/v1/agent/planner/sessions/{session_id}/steps", ("POST",)) in paths


# --- Endpoint tests with mocks -----------------------------------------


@pytest.mark.asyncio
async def test_create_session_returns_public_shape(monkeypatch):
    row = {
        "id": "planner_1", "workspace_id": "wsp_test", "actor_id": "usr_test",
        "status": "active", "context": {}, "plan": {}, "iterations": 0,
        "budget_used": 0, "budget_limit": 100, "metadata": {},
        "created_at": None, "updated_at": None,
    }
    conn = _SeqConnection(results=[_Result(row=row)])
    monkeypatch.setattr(agent_planner, "pool", _Pool(conn))

    result = await agent_planner.create_session(agent_planner.PlannerSessionCreate(budget_limit=100), _actor())
    assert result["id"] == "planner_1"
    assert result["budget_limit"] == 100.0
    assert result["budget_used"] == 0.0


@pytest.mark.asyncio
async def test_list_sessions_is_workspace_scoped(monkeypatch):
    rows = [{"id": "planner_1", "budget_used": 0, "budget_limit": 0}]
    conn = _SeqConnection(results=[_Result(rows=rows), _Result(row={"total": 1})])
    monkeypatch.setattr(agent_planner, "pool", _Pool(conn))

    result = await agent_planner.list_sessions(_actor(), limit=10, offset=0)
    assert result["items"][0]["id"] == "planner_1"
    assert result["total"] == 1
    query, params = conn.calls[0]
    assert "workspace_id=%s" in query
    assert params[0] == "wsp_test"


@pytest.mark.asyncio
async def test_get_session_returns_row(monkeypatch):
    row = {"id": "planner_1", "budget_used": 10, "budget_limit": 50}
    conn = _SeqConnection(results=[_Result(row=row)])
    monkeypatch.setattr(agent_planner, "pool", _Pool(conn))

    result = await agent_planner.get_session("planner_1", _actor())
    assert result["id"] == "planner_1"
    assert result["budget_used"] == 10.0


@pytest.mark.asyncio
async def test_get_session_returns_404_when_missing(monkeypatch):
    conn = _SeqConnection(results=[_Result(row=None)])
    monkeypatch.setattr(agent_planner, "pool", _Pool(conn))

    with pytest.raises(HTTPException) as exc:
        await agent_planner.get_session("planner_missing", _actor())
    assert exc.value.status_code == 404


@pytest.mark.asyncio
async def test_delete_session_completes_and_returns_status(monkeypatch):
    conn = _SeqConnection(results=[_Result(row={"id": "planner_1"})])
    monkeypatch.setattr(agent_planner, "pool", _Pool(conn))

    result = await agent_planner.delete_session("planner_1", _actor())
    assert result["status"] == "completed"
    query, _ = conn.calls[0]
    assert "status='completed'" in query


@pytest.mark.asyncio
async def test_delete_session_returns_404_when_missing(monkeypatch):
    conn = _SeqConnection(results=[_Result(row=None)])
    monkeypatch.setattr(agent_planner, "pool", _Pool(conn))

    with pytest.raises(HTTPException) as exc:
        await agent_planner.delete_session("planner_missing", _actor())
    assert exc.value.status_code == 404


@pytest.mark.asyncio
async def test_add_step_increments_budget_and_order(monkeypatch):
    session_row = {"id": "planner_1", "status": "active", "budget_used": 5.0, "budget_limit": 100.0}
    order_row = {"next_order": 2}
    step_row = {
        "id": "plstep_1", "session_id": "planner_1", "step_order": 2,
        "action": "run tool", "tool_calls": [], "observations": [],
        "next_choices": [], "status": "completed", "cost": 3.0, "metadata": {},
        "created_at": None,
    }
    conn = _SeqConnection(results=[_Result(row=session_row), _Result(row=order_row), _Result(row=step_row), _Result()])
    monkeypatch.setattr(agent_planner, "pool", _Pool(conn))

    result = await agent_planner.add_step(
        "planner_1", agent_planner.PlannerStepCreate(action="run tool", cost=3.0), _actor()
    )
    assert result["step_order"] == 2
    assert result["cost"] == 3.0
    # UPDATE session budget
    update_query, update_params = conn.calls[3]
    assert "budget_used=%s" in update_query
    assert update_params[0] == 8.0


@pytest.mark.asyncio
async def test_add_step_rejects_inactive_session(monkeypatch):
    session_row = {"id": "planner_1", "status": "completed", "budget_used": 0, "budget_limit": 0}
    conn = _SeqConnection(results=[_Result(row=session_row)])
    monkeypatch.setattr(agent_planner, "pool", _Pool(conn))

    with pytest.raises(HTTPException) as exc:
        await agent_planner.add_step(
            "planner_1", agent_planner.PlannerStepCreate(action="x"), _actor()
        )
    assert exc.value.status_code == 409
    assert "not active" in exc.value.detail


@pytest.mark.asyncio
async def test_add_step_returns_404_when_session_missing(monkeypatch):
    conn = _SeqConnection(results=[_Result(row=None)])
    monkeypatch.setattr(agent_planner, "pool", _Pool(conn))

    with pytest.raises(HTTPException) as exc:
        await agent_planner.add_step(
            "planner_missing", agent_planner.PlannerStepCreate(action="x"), _actor()
        )
    assert exc.value.status_code == 404


@pytest.mark.asyncio
async def test_list_steps_returns_ordered_rows(monkeypatch):
    rows = [
        {"id": "plstep_1", "step_order": 0, "cost": 1.0},
        {"id": "plstep_2", "step_order": 1, "cost": 2.0},
    ]
    conn = _SeqConnection(results=[_Result(rows=rows)])
    monkeypatch.setattr(agent_planner, "pool", _Pool(conn))

    result = await agent_planner.list_steps("planner_1", _actor(), limit=10, offset=0)
    assert len(result["items"]) == 2
    assert result["items"][0]["cost"] == 1.0
    query, params = conn.calls[0]
    assert "session_id=%s" in query
    assert params[0] == "planner_1"


def test_planner_step_create_normalizes_action():
    body = agent_planner.PlannerStepCreate(action="  do something  ")
    assert body.action == "do something"
