from __future__ import annotations

import asyncio
import hashlib
import inspect
import json
from dataclasses import dataclass, field, replace
from enum import StrEnum
from typing import Any, Awaitable, Callable, Mapping, Protocol, Sequence

from workama_agent.planner import (
    Budget,
    BudgetUsage,
    ConvergenceDecision,
    ConvergenceReason,
    PlannerError,
    PlannerLimits,
    TaskBudget,
    TaskPlan,
    TaskSpec,
    TaskStatus,
    allocate_task_budgets,
    blocked_task_ids,
    convergence_decision,
    progress_fingerprint,
    ready_task_ids,
    validate_plan,
)


class ExecutorMessageType(StrEnum):
    TASK_ASSIGNED = "task.assigned"
    TASK_ACCEPTED = "task.accepted"
    TASK_PROGRESS = "task.progress"
    TASK_COMPLETED = "task.completed"
    TASK_FAILED = "task.failed"
    TASK_CANCELLED = "task.cancelled"


class CoordinationStatus(StrEnum):
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    STOPPED = "stopped"


def _required_text(value: Any, field_name: str, *, max_length: int = 256) -> str:
    result = str(value or "").strip()
    if not result or len(result) > max_length or "\x00" in result:
        raise PlannerError("E00001", f"{field_name} is invalid")
    return result


def _stable_id(prefix: str, *parts: str) -> str:
    material = "\x1f".join(parts).encode("utf-8")
    return f"{prefix}_{hashlib.sha256(material).hexdigest()[:24]}"


@dataclass(frozen=True, slots=True)
class SubsessionRequest:
    request_id: str
    parent_session_id: str
    child_session_id: str
    workspace_id: str
    task_id: str
    objective: str
    budget: TaskBudget
    idempotency_key: str
    depth: int
    executor: str = "default"
    context_refs: tuple[str, ...] = ()
    capabilities: tuple[str, ...] = ()
    actor_ref: str | None = None
    trace_id: str | None = None
    schema_version: str = "1.0"

    def __post_init__(self) -> None:
        for field_name in ("request_id", "parent_session_id", "child_session_id", "workspace_id", "task_id", "idempotency_key"):
            object.__setattr__(self, field_name, _required_text(getattr(self, field_name), field_name))
        object.__setattr__(self, "objective", _required_text(self.objective, "objective", max_length=4000))
        object.__setattr__(self, "executor", _required_text(self.executor, "executor"))
        if self.depth < 1:
            raise PlannerError("E07006", "subsession depth must be at least 1")
        object.__setattr__(self, "context_refs", tuple(str(value) for value in self.context_refs))
        object.__setattr__(self, "capabilities", tuple(str(value) for value in self.capabilities))
        if self.actor_ref is not None:
            object.__setattr__(self, "actor_ref", _required_text(self.actor_ref, "actor_ref"))
        if self.trace_id is not None:
            object.__setattr__(self, "trace_id", _required_text(self.trace_id, "trace_id"))

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema_version": self.schema_version,
            "request_id": self.request_id,
            "parent_session_id": self.parent_session_id,
            "child_session_id": self.child_session_id,
            "workspace_id": self.workspace_id,
            "task_id": self.task_id,
            "objective": self.objective,
            "budget": self.budget.to_dict(),
            "idempotency_key": self.idempotency_key,
            "depth": self.depth,
            "executor": self.executor,
            "context_refs": list(self.context_refs),
            "capabilities": list(self.capabilities),
            "actor_ref": self.actor_ref,
            "trace_id": self.trace_id,
        }


@dataclass(frozen=True, slots=True)
class ExecutorResult:
    task_id: str
    status: TaskStatus | str = TaskStatus.SUCCEEDED
    summary: str = ""
    output_ref: str | None = None
    usage: BudgetUsage = field(default_factory=BudgetUsage)
    progress_marker: str | None = None
    error_code: str | None = None
    metadata: Mapping[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        object.__setattr__(self, "task_id", _required_text(self.task_id, "task_id"))
        try:
            normalized = TaskStatus(str(self.status))
        except ValueError as exc:
            raise PlannerError("E07006", "executor result has an unsupported status") from exc
        if normalized not in {TaskStatus.SUCCEEDED, TaskStatus.FAILED, TaskStatus.CANCELLED}:
            raise PlannerError("E07006", "executor result must be terminal")
        object.__setattr__(self, "status", normalized)
        object.__setattr__(self, "summary", str(self.summary or "")[:2000])
        if self.output_ref is not None:
            object.__setattr__(self, "output_ref", _required_text(self.output_ref, "output_ref"))
        if self.progress_marker is not None:
            object.__setattr__(self, "progress_marker", str(self.progress_marker)[:256])
        if self.error_code is not None:
            object.__setattr__(self, "error_code", _required_text(self.error_code, "error_code"))
        if not isinstance(self.metadata, Mapping):
            raise PlannerError("E00001", "executor result metadata must be an object")

    @classmethod
    def from_value(cls, value: "ExecutorResult | Mapping[str, Any]") -> "ExecutorResult":
        if isinstance(value, cls):
            return value
        if not isinstance(value, Mapping):
            raise PlannerError("E07006", "executor returned an invalid result")
        raw_usage = value.get("usage", {})
        if isinstance(raw_usage, BudgetUsage):
            usage = raw_usage
        elif isinstance(raw_usage, Mapping):
            usage = BudgetUsage(int(raw_usage.get("steps", 0)), float(raw_usage.get("credits", 0.0)))
        else:
            raise PlannerError("E07006", "executor result usage must be an object")
        return cls(
            task_id=str(value.get("task_id", "")),
            status=str(value.get("status", TaskStatus.SUCCEEDED.value)),
            summary=str(value.get("summary", "")),
            output_ref=value.get("output_ref"),
            usage=usage,
            progress_marker=value.get("progress_marker"),
            error_code=value.get("error_code"),
            metadata=value.get("metadata", {}),
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "task_id": self.task_id,
            "status": self.status.value,
            "summary": self.summary,
            "output_ref": self.output_ref,
            "usage": self.usage.to_dict(),
            "progress_marker": self.progress_marker,
            "error_code": self.error_code,
            "metadata": dict(self.metadata),
        }


@dataclass(frozen=True, slots=True)
class ExecutorMessage:
    event_type: ExecutorMessageType | str
    message_id: str
    request_id: str
    task_id: str
    parent_session_id: str
    child_session_id: str
    workspace_id: str
    payload: Mapping[str, Any]
    producer: str = "agent-coordinator"
    idempotency_key: str | None = None
    actor_ref: str | None = None
    trace_id: str | None = None
    classification: str = "C2"
    schema_version: str = "1.0"
    occurred_at: str | None = None

    def __post_init__(self) -> None:
        try:
            object.__setattr__(self, "event_type", ExecutorMessageType(str(self.event_type)))
        except ValueError as exc:
            raise PlannerError("E07006", "unsupported executor message type") from exc
        for field_name in ("message_id", "request_id", "task_id", "parent_session_id", "child_session_id", "workspace_id"):
            object.__setattr__(self, field_name, _required_text(getattr(self, field_name), field_name))
        if not isinstance(self.payload, Mapping):
            raise PlannerError("E00001", "executor message payload must be an object")

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema_version": self.schema_version,
            "event_id": self.message_id,
            "message_id": self.message_id,
            "event_type": self.event_type.value,
            "producer": self.producer,
            "workspace_id": self.workspace_id,
            "parent_session_id": self.parent_session_id,
            "child_session_id": self.child_session_id,
            "task_id": self.task_id,
            "request_id": self.request_id,
            "actor_ref": self.actor_ref,
            "trace_id": self.trace_id,
            "idempotency_key": self.idempotency_key,
            "classification": self.classification,
            "occurred_at": self.occurred_at,
            "payload": dict(self.payload),
        }


@dataclass(slots=True)
class TaskExecutionState:
    task_id: str
    status: TaskStatus = TaskStatus.PENDING
    request_id: str | None = None
    child_session_id: str | None = None
    attempt: int = 0
    result: ExecutorResult | None = None
    error: str | None = None
    progress_marker: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "task_id": self.task_id,
            "status": self.status.value,
            "request_id": self.request_id,
            "child_session_id": self.child_session_id,
            "attempt": self.attempt,
            "result": self.result.to_dict() if self.result else None,
            "error": self.error,
            "progress_marker": self.progress_marker,
        }


@dataclass(frozen=True, slots=True)
class CoordinationResult:
    status: CoordinationStatus
    plan_id: str
    reason: ConvergenceReason
    message: str
    usage: Budget
    states: tuple[TaskExecutionState, ...]
    messages: tuple[ExecutorMessage, ...]
    progress_history: tuple[str, ...]

    def to_dict(self) -> dict[str, Any]:
        return {
            "status": self.status.value,
            "plan_id": self.plan_id,
            "reason": self.reason.value,
            "message": self.message,
            "usage": self.usage.to_dict(),
            "states": [state.to_dict() for state in self.states],
            "messages": [message.to_dict() for message in self.messages],
            "progress_history": list(self.progress_history),
        }


ProgressEmitter = Callable[[Mapping[str, Any]], Awaitable[None]]
MessageSink = Callable[[ExecutorMessage], Awaitable[None] | None]
ChildSessionFactory = Callable[[SubsessionRequest], str | Awaitable[str]]


class Executor(Protocol):
    async def execute(self, request: SubsessionRequest, emit: ProgressEmitter) -> ExecutorResult | Mapping[str, Any]: ...


class _BudgetLedger:
    def __init__(self, budget: Budget):
        self._max_steps = budget.max_steps
        self._max_credits = budget.max_credits
        self._used_steps = budget.used_steps
        self._used_credits = budget.used_credits
        self._reserved_steps = 0
        self._reserved_credits = 0.0
        self._lock = asyncio.Lock()

    async def reserve(self, grant: TaskBudget) -> bool:
        async with self._lock:
            if self._used_steps + self._reserved_steps + grant.max_steps > self._max_steps:
                return False
            if self._used_credits + self._reserved_credits + grant.max_credits > self._max_credits:
                return False
            self._reserved_steps += grant.max_steps
            self._reserved_credits += grant.max_credits
            return True

    async def settle(self, grant: TaskBudget, usage: BudgetUsage) -> bool:
        async with self._lock:
            self._reserved_steps = max(self._reserved_steps - grant.max_steps, 0)
            self._reserved_credits = max(self._reserved_credits - grant.max_credits, 0.0)
            within_grant = usage.steps <= grant.max_steps and usage.credits <= grant.max_credits
            within_parent = self._used_steps + usage.steps <= self._max_steps and self._used_credits + usage.credits <= self._max_credits
            self._used_steps = min(self._max_steps, self._used_steps + usage.steps)
            self._used_credits = min(self._max_credits, self._used_credits + usage.credits)
            return within_grant and within_parent

    def snapshot(self) -> Budget:
        return Budget(self._max_steps, self._max_credits, self._used_steps, self._used_credits)


class Coordinator:
    """Run a bounded planner/executor graph with child-session message contracts."""

    def __init__(
        self,
        executor: Executor,
        *,
        limits: PlannerLimits | None = None,
        message_sink: MessageSink | None = None,
        child_session_factory: ChildSessionFactory | None = None,
    ):
        self.executor = executor
        self.limits = limits or PlannerLimits()
        self.message_sink = message_sink
        self.child_session_factory = child_session_factory

    def _effective_limits(self, plan: TaskPlan) -> PlannerLimits:
        return PlannerLimits(
            max_steps=min(plan.limits.max_steps, self.limits.max_steps),
            max_credits=min(plan.limits.max_credits, self.limits.max_credits),
            max_depth=min(plan.limits.max_depth, self.limits.max_depth),
            max_concurrency=min(plan.limits.max_concurrency, self.limits.max_concurrency),
            max_agents=min(plan.limits.max_agents, self.limits.max_agents),
            no_progress_rounds=min(plan.limits.no_progress_rounds, self.limits.no_progress_rounds),
        )

    async def _emit(
        self,
        messages: list[ExecutorMessage],
        message: ExecutorMessage,
    ) -> None:
        messages.append(message)
        if self.message_sink is None:
            return
        result = self.message_sink(message)
        if inspect.isawaitable(result):
            await result

    async def _run_task(
        self,
        task: TaskSpec,
        task_budget: TaskBudget,
        *,
        parent_session_id: str,
        workspace_id: str,
        depth: int,
        run_id: str,
        context_refs: tuple[str, ...],
        capabilities: tuple[str, ...],
        actor_ref: str | None,
        trace_id: str | None,
        ledger: _BudgetLedger,
        messages: list[ExecutorMessage],
        state: TaskExecutionState,
    ) -> None:
        request_id = _stable_id("req", run_id, task.id, str(state.attempt))
        child_session_id = _stable_id("ses", parent_session_id, task.id, str(state.attempt))
        request = SubsessionRequest(
            request_id=request_id,
            parent_session_id=parent_session_id,
            child_session_id=child_session_id,
            workspace_id=workspace_id,
            task_id=task.id,
            objective=task.objective,
            budget=task_budget,
            idempotency_key=task.effective_idempotency_key,
            depth=max(depth + 1, task.depth),
            executor=task.executor,
            context_refs=tuple(dict.fromkeys((*context_refs, *task.context_refs))),
            capabilities=tuple(dict.fromkeys((*capabilities, *task.capabilities))),
            actor_ref=actor_ref,
            trace_id=trace_id,
        )
        state.request_id = request.request_id
        state.child_session_id = request.child_session_id

        if self.child_session_factory is not None:
            created = self.child_session_factory(request)
            if inspect.isawaitable(created):
                created = await created
            created_id = _required_text(created, "child_session_id")
            request = replace(request, child_session_id=created_id)
            state.child_session_id = created_id

        if not await ledger.reserve(task_budget):
            state.status = TaskStatus.BLOCKED
            state.error = "parent budget cannot reserve this child task"
            await self._emit(
                messages,
                self._message(request, ExecutorMessageType.TASK_FAILED, {"error_code": "E04002", "summary": state.error}, 0),
            )
            return

        await self._emit(
            messages,
            self._message(
                request,
                ExecutorMessageType.TASK_ASSIGNED,
                {
                    "task_id": task.id,
                    "objective": task.objective,
                    "dependencies": list(task.dependencies),
                    "budget": task_budget.to_dict(),
                    "context_refs": list(request.context_refs),
                    "capabilities": list(request.capabilities),
                },
                0,
            ),
        )
        await self._emit(
            messages,
            self._message(request, ExecutorMessageType.TASK_ACCEPTED, {"status": "accepted"}, 1),
        )

        progress_index = 1

        async def emit_progress(payload: Mapping[str, Any]) -> None:
            nonlocal progress_index
            progress_index += 1
            data = dict(payload)
            marker = data.get("progress_marker")
            if marker is not None:
                state.progress_marker = str(marker)[:256]
            await self._emit(messages, self._message(request, ExecutorMessageType.TASK_PROGRESS, data, progress_index))

        try:
            result_value = self.executor.execute(request, emit_progress)
            if inspect.isawaitable(result_value):
                result_value = await result_value
            result = ExecutorResult.from_value(result_value)
            if result.task_id != task.id:
                raise PlannerError("E07006", "executor result task_id does not match assignment")
            settled = await ledger.settle(task_budget, result.usage)
            if not settled:
                result = ExecutorResult(
                    task_id=task.id,
                    status=TaskStatus.FAILED,
                    summary="executor exceeded the assigned child budget",
                    usage=result.usage,
                    progress_marker=result.progress_marker,
                    error_code="E04002",
                )
                state.error = result.summary
            state.result = result
            state.progress_marker = result.progress_marker
            state.status = result.status
            event_type = {
                TaskStatus.SUCCEEDED: ExecutorMessageType.TASK_COMPLETED,
                TaskStatus.FAILED: ExecutorMessageType.TASK_FAILED,
                TaskStatus.CANCELLED: ExecutorMessageType.TASK_CANCELLED,
            }[result.status]
            await self._emit(messages, self._message(request, event_type, result.to_dict(), progress_index + 1))
        except asyncio.CancelledError:
            await ledger.settle(task_budget, BudgetUsage())
            state.status = TaskStatus.CANCELLED
            state.error = "coordinator cancelled child execution"
            await self._emit(
                messages,
                self._message(request, ExecutorMessageType.TASK_CANCELLED, {"summary": state.error}, progress_index + 1),
            )
            raise
        except Exception as exc:
            await ledger.settle(task_budget, BudgetUsage())
            state.status = TaskStatus.FAILED
            state.error = str(exc)[:1000]
            state.result = ExecutorResult(
                task_id=task.id,
                status=TaskStatus.FAILED,
                summary=state.error,
                error_code=getattr(exc, "code", "E07001"),
            )
            await self._emit(
                messages,
                self._message(request, ExecutorMessageType.TASK_FAILED, state.result.to_dict(), progress_index + 1),
            )

    def _message(
        self,
        request: SubsessionRequest,
        event_type: ExecutorMessageType,
        payload: Mapping[str, Any],
        sequence: int,
    ) -> ExecutorMessage:
        return ExecutorMessage(
            event_type=event_type,
            message_id=_stable_id("msg", request.request_id, event_type.value, str(sequence)),
            request_id=request.request_id,
            task_id=request.task_id,
            parent_session_id=request.parent_session_id,
            child_session_id=request.child_session_id,
            workspace_id=request.workspace_id,
            payload=dict(payload),
            idempotency_key=request.idempotency_key,
            actor_ref=request.actor_ref,
            trace_id=request.trace_id,
        )

    @staticmethod
    def _result_status(decision: ConvergenceDecision) -> CoordinationStatus:
        if decision.reason == ConvergenceReason.COMPLETE:
            return CoordinationStatus.SUCCEEDED
        if decision.reason in {ConvergenceReason.FAILED, ConvergenceReason.BLOCKED}:
            return CoordinationStatus.FAILED
        return CoordinationStatus.STOPPED

    async def run(
        self,
        plan: TaskPlan,
        *,
        parent_session_id: str,
        workspace_id: str,
        parent_budget: Budget | None = None,
        depth: int = 0,
        context_refs: Sequence[str] = (),
        capabilities: Sequence[str] = (),
        actor_ref: str | None = None,
        trace_id: str | None = None,
        run_id: str | None = None,
    ) -> CoordinationResult:
        limits = self._effective_limits(plan)
        validate_plan(plan.tasks, limits)
        parent_session_id = _required_text(parent_session_id, "parent_session_id")
        workspace_id = _required_text(workspace_id, "workspace_id")
        if depth < 0:
            raise PlannerError("E07006", "coordination depth cannot be negative")
        run_id = run_id or _stable_id("run", plan.id, parent_session_id)
        states = {task.id: TaskExecutionState(task.id) for task in plan.tasks}
        messages: list[ExecutorMessage] = []
        progress_history: list[str] = []
        if depth >= limits.max_depth:
            decision = ConvergenceDecision(True, ConvergenceReason.DEPTH_LIMIT, "child depth limit reached", 0, len(states))
            return CoordinationResult(CoordinationStatus.STOPPED, plan.id, decision.reason, decision.message, Budget(limits.max_steps, limits.max_credits), tuple(states.values()), tuple(messages), tuple(progress_history))

        initial = parent_budget or Budget(limits.max_steps, limits.max_credits)
        bounded_budget = Budget(
            min(initial.max_steps, limits.max_steps),
            min(initial.max_credits, limits.max_credits),
            initial.used_steps,
            initial.used_credits,
        )
        try:
            task_budgets = allocate_task_budgets(plan.tasks, bounded_budget)
        except PlannerError as exc:
            reason = ConvergenceReason.MAX_STEPS if exc.code == "E04003" else ConvergenceReason.BUDGET_EXHAUSTED
            decision = ConvergenceDecision(True, reason, str(exc), 0, len(states))
            return CoordinationResult(CoordinationStatus.STOPPED, plan.id, decision.reason, decision.message, bounded_budget, tuple(states.values()), tuple(messages), tuple(progress_history))

        ledger = _BudgetLedger(bounded_budget)
        launched = 0
        while True:
            for task_id in blocked_task_ids(plan, states):
                states[task_id].status = TaskStatus.BLOCKED
                states[task_id].error = "dependency did not succeed"
            decision = convergence_decision(plan, states, ledger.snapshot(), progress_history)
            if decision.should_stop:
                if decision.reason in {ConvergenceReason.NO_PROGRESS, ConvergenceReason.BUDGET_EXHAUSTED, ConvergenceReason.MAX_STEPS, ConvergenceReason.AGENT_LIMIT}:
                    for state in states.values():
                        if state.status == TaskStatus.PENDING:
                            state.status = TaskStatus.BLOCKED
                            state.error = decision.message
                break
            ready = ready_task_ids(plan, states)
            available = min(limits.max_concurrency, limits.max_agents - launched)
            if available <= 0:
                decision = ConvergenceDecision(True, ConvergenceReason.AGENT_LIMIT, "maximum child agent count reached", decision.completed, decision.remaining)
                for state in states.values():
                    if state.status == TaskStatus.PENDING:
                        state.status = TaskStatus.BLOCKED
                        state.error = decision.message
                break
            batch = ready[:available]
            if not batch:
                decision = ConvergenceDecision(True, ConvergenceReason.BLOCKED, "no runnable task remains", decision.completed, decision.remaining)
                break
            running: list[asyncio.Task[None]] = []
            for task_id in batch:
                state = states[task_id]
                state.status = TaskStatus.RUNNING
                state.attempt += 1
                task = next(item for item in plan.tasks if item.id == task_id)
                running.append(
                    asyncio.create_task(
                        self._run_task(
                            task,
                            task_budgets[task_id],
                            parent_session_id=parent_session_id,
                            workspace_id=workspace_id,
                            depth=depth,
                            run_id=run_id,
                            context_refs=tuple(context_refs),
                            capabilities=tuple(capabilities),
                            actor_ref=actor_ref,
                            trace_id=trace_id,
                            ledger=ledger,
                            messages=messages,
                            state=state,
                        )
                    )
                )
            launched += len(batch)
            await asyncio.gather(*running, return_exceptions=True)
            progress_history.append(progress_fingerprint(states))

        final_budget = ledger.snapshot()
        return CoordinationResult(
            self._result_status(decision),
            plan.id,
            decision.reason,
            decision.message,
            final_budget,
            tuple(states.values()),
            tuple(messages),
            tuple(progress_history),
        )
