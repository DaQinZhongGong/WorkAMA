import asyncio

import pytest

from workama_agent.coordination import (
    CoordinationStatus,
    Coordinator,
    ExecutorMessage,
    ExecutorMessageType,
    ExecutorResult,
    SubsessionRequest,
)
from workama_agent.planner import Budget, BudgetUsage, ConvergenceReason, PlannerLimits, TaskBudget, TaskStatus, decompose_tasks


class FakeExecutor:
    def __init__(self, *, failing: set[str] | None = None, over_budget: set[str] | None = None):
        self.failing = failing or set()
        self.over_budget = over_budget or set()
        self.started: list[str] = []
        self.max_active = 0
        self.active = 0
        self.requests: list[SubsessionRequest] = []

    async def execute(self, request: SubsessionRequest, emit):
        self.requests.append(request)
        self.started.append(request.task_id)
        self.active += 1
        self.max_active = max(self.max_active, self.active)
        await emit({"progress_marker": f"started:{request.task_id}", "progress": 0.5})
        await asyncio.sleep(0)
        self.active -= 1
        if request.task_id in self.failing:
            return ExecutorResult(
                request.task_id,
                status=TaskStatus.FAILED,
                summary="executor failed",
                usage=BudgetUsage(1, 1),
                error_code="E07001",
            )
        usage = BudgetUsage(2, 1) if request.task_id in self.over_budget else BudgetUsage(1, 1)
        return ExecutorResult(
            request.task_id,
            status=TaskStatus.SUCCEEDED,
            summary="done",
            output_ref=f"artifact:{request.task_id}",
            usage=usage,
            progress_marker=f"done:{request.task_id}",
        )


def _plan():
    return decompose_tasks(
        "Build a report",
        [
            {"id": "research", "objective": "Research sources"},
            {"id": "draft", "objective": "Draft analysis"},
            {"id": "review", "objective": "Review the report", "dependencies": ["research", "draft"]},
        ],
        limits=PlannerLimits(max_steps=10, max_credits=10, max_concurrency=2, max_agents=5),
    )


@pytest.mark.asyncio
async def test_coordinator_runs_dependency_waves_with_bounded_concurrency_and_messages():
    executor = FakeExecutor()
    received: list[ExecutorMessage] = []

    async def sink(message: ExecutorMessage):
        received.append(message)

    async def create_child(request: SubsessionRequest) -> str:
        return f"child:{request.task_id}"

    result = await Coordinator(executor, message_sink=sink, child_session_factory=create_child).run(
        _plan(),
        parent_session_id="ses_parent",
        workspace_id="wsp_test",
        context_refs=["session:summary"],
        capabilities=["research:read"],
        run_id="run_test",
    )

    assert result.status == CoordinationStatus.SUCCEEDED
    assert result.reason == ConvergenceReason.COMPLETE
    assert executor.max_active == 2
    assert executor.started[:2] == ["research", "draft"]
    assert executor.started[2:] == ["review"]
    assert all(state.status == TaskStatus.SUCCEEDED for state in result.states)
    assert all(request.depth == 1 for request in executor.requests)
    assert executor.requests[0].child_session_id.startswith("child:")
    assert executor.requests[0].context_refs == ("session:summary",)
    event_types = [message.event_type for message in received]
    assert ExecutorMessageType.TASK_ASSIGNED in event_types
    assert ExecutorMessageType.TASK_ACCEPTED in event_types
    assert ExecutorMessageType.TASK_PROGRESS in event_types
    assert ExecutorMessageType.TASK_COMPLETED in event_types
    assert all(message.workspace_id == "wsp_test" for message in received)


@pytest.mark.asyncio
async def test_failed_task_blocks_dependents_without_running_them():
    executor = FakeExecutor(failing={"research"})
    result = await Coordinator(executor).run(
        _plan(), parent_session_id="ses_parent", workspace_id="wsp_test", run_id="run_failure"
    )

    assert result.status == CoordinationStatus.FAILED
    assert result.reason == ConvergenceReason.FAILED
    assert executor.started == ["research", "draft"]
    states = {state.task_id: state for state in result.states}
    assert states["research"].status == TaskStatus.FAILED
    assert states["review"].status == TaskStatus.BLOCKED


@pytest.mark.asyncio
async def test_parent_budget_rejects_plan_before_spawning_children():
    executor = FakeExecutor()
    result = await Coordinator(executor).run(
        _plan(),
        parent_session_id="ses_parent",
        workspace_id="wsp_test",
        parent_budget=Budget(max_steps=2, max_credits=10),
    )

    assert result.status == CoordinationStatus.STOPPED
    assert result.reason == ConvergenceReason.MAX_STEPS
    assert executor.started == []


@pytest.mark.asyncio
async def test_executor_budget_overrun_is_failed_and_counted():
    plan = decompose_tasks(
        "Goal",
        [{"id": "expensive", "objective": "Run bounded work"}],
        limits=PlannerLimits(max_steps=4, max_credits=4),
    )
    executor = FakeExecutor(over_budget={"expensive"})
    result = await Coordinator(executor).run(plan, parent_session_id="ses_parent", workspace_id="wsp_test")

    assert result.status == CoordinationStatus.FAILED
    state = result.states[0]
    assert state.status == TaskStatus.FAILED
    assert state.result is not None and state.result.error_code == "E04002"
    assert result.usage.used_steps == 2


@pytest.mark.asyncio
async def test_depth_limit_stops_without_executor_call():
    executor = FakeExecutor()
    result = await Coordinator(executor).run(
        _plan(), parent_session_id="ses_parent", workspace_id="wsp_test", depth=2
    )

    assert result.status == CoordinationStatus.STOPPED
    assert result.reason == ConvergenceReason.DEPTH_LIMIT
    assert executor.started == []


def test_subsession_and_executor_message_contracts_are_serializable():
    request = SubsessionRequest(
        request_id="req_1",
        parent_session_id="ses_parent",
        child_session_id="ses_child",
        workspace_id="wsp_test",
        task_id="task_1",
        objective="Inspect the source",
        budget=TaskBudget(3, 2.5),
        idempotency_key="task-key",
        depth=1,
        context_refs=("session:summary",),
        capabilities=("file.read",),
    )
    message = ExecutorMessage(
        event_type=ExecutorMessageType.TASK_ASSIGNED,
        message_id="msg_1",
        request_id=request.request_id,
        task_id=request.task_id,
        parent_session_id=request.parent_session_id,
        child_session_id=request.child_session_id,
        workspace_id=request.workspace_id,
        payload={"budget": request.budget.to_dict()},
    )

    encoded = message.to_dict()
    assert request.to_dict()["parent_session_id"] == "ses_parent"
    assert encoded["event_id"] == "msg_1"
    assert encoded["event_type"] == "task.assigned"
    assert encoded["classification"] == "C2"
