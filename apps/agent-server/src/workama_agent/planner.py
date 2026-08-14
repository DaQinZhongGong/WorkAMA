from __future__ import annotations

import hashlib
import json
import math
from dataclasses import dataclass, field, replace
from enum import StrEnum
from typing import Any, Iterable, Mapping, Sequence


GLOBAL_MAX_DEPTH = 2
GLOBAL_MAX_CONCURRENCY = 3
GLOBAL_MAX_AGENTS = 8


class PlannerError(ValueError):
    """A deterministic, user-safe planning validation error."""

    def __init__(self, code: str, message: str):
        self.code = code
        super().__init__(message)


class TaskStatus(StrEnum):
    PENDING = "pending"
    READY = "ready"
    RUNNING = "running"
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    BLOCKED = "blocked"
    CANCELLED = "cancelled"


class ConvergenceReason(StrEnum):
    CONTINUE = "continue"
    COMPLETE = "complete"
    BUDGET_EXHAUSTED = "budget_exhausted"
    MAX_STEPS = "max_steps"
    NO_PROGRESS = "no_progress"
    FAILED = "failed"
    BLOCKED = "blocked"
    AGENT_LIMIT = "agent_limit"
    DEPTH_LIMIT = "depth_limit"


@dataclass(frozen=True, slots=True)
class PlannerLimits:
    max_steps: int = 50
    max_credits: float = 500.0
    max_depth: int = GLOBAL_MAX_DEPTH
    max_concurrency: int = GLOBAL_MAX_CONCURRENCY
    max_agents: int = GLOBAL_MAX_AGENTS
    no_progress_rounds: int = 3

    def __post_init__(self) -> None:
        if isinstance(self.max_steps, bool) or self.max_steps < 1:
            raise PlannerError("E04003", "max_steps must be at least 1")
        if not math.isfinite(float(self.max_credits)) or self.max_credits <= 0:
            raise PlannerError("E04002", "max_credits must be greater than 0")
        if self.max_depth < 0 or self.max_depth > GLOBAL_MAX_DEPTH:
            raise PlannerError("E07006", f"max_depth must be between 0 and {GLOBAL_MAX_DEPTH}")
        if self.max_concurrency < 1 or self.max_concurrency > GLOBAL_MAX_CONCURRENCY:
            raise PlannerError("E07006", f"max_concurrency must be between 1 and {GLOBAL_MAX_CONCURRENCY}")
        if self.max_agents < 1 or self.max_agents > GLOBAL_MAX_AGENTS:
            raise PlannerError("E07006", f"max_agents must be between 1 and {GLOBAL_MAX_AGENTS}")
        if self.no_progress_rounds < 1:
            raise PlannerError("E07006", "no_progress_rounds must be at least 1")


@dataclass(frozen=True, slots=True)
class BudgetUsage:
    steps: int = 0
    credits: float = 0.0

    def __post_init__(self) -> None:
        if isinstance(self.steps, bool) or self.steps < 0:
            raise PlannerError("E04003", "usage steps cannot be negative")
        if not math.isfinite(float(self.credits)) or self.credits < 0:
            raise PlannerError("E04002", "usage credits cannot be negative")

    def __add__(self, other: "BudgetUsage") -> "BudgetUsage":
        return BudgetUsage(self.steps + other.steps, self.credits + other.credits)

    def to_dict(self) -> dict[str, int | float]:
        return {"steps": self.steps, "credits": self.credits}


@dataclass(frozen=True, slots=True)
class Budget:
    max_steps: int
    max_credits: float
    used_steps: int = 0
    used_credits: float = 0.0

    def __post_init__(self) -> None:
        if isinstance(self.max_steps, bool) or self.max_steps < 1:
            raise PlannerError("E04003", "budget max_steps must be at least 1")
        if not math.isfinite(float(self.max_credits)) or self.max_credits <= 0:
            raise PlannerError("E04002", "budget max_credits must be greater than 0")
        if self.used_steps < 0 or self.used_steps > self.max_steps:
            raise PlannerError("E04003", "budget used_steps is outside its limit")
        if self.used_credits < 0 or self.used_credits > self.max_credits:
            raise PlannerError("E04002", "budget used_credits is outside its limit")

    @property
    def remaining_steps(self) -> int:
        return max(self.max_steps - self.used_steps, 0)

    @property
    def remaining_credits(self) -> float:
        return max(self.max_credits - self.used_credits, 0.0)

    @property
    def exhausted(self) -> bool:
        return self.remaining_steps <= 0 or self.remaining_credits <= 0

    def can_consume(self, usage: BudgetUsage) -> bool:
        return usage.steps <= self.remaining_steps and usage.credits <= self.remaining_credits

    def consume(self, usage: BudgetUsage) -> "Budget":
        if not self.can_consume(usage):
            raise PlannerError("E04002", "budget would be exceeded")
        return replace(
            self,
            used_steps=self.used_steps + usage.steps,
            used_credits=self.used_credits + usage.credits,
        )

    def to_dict(self) -> dict[str, int | float]:
        return {
            "max_steps": self.max_steps,
            "max_credits": self.max_credits,
            "used_steps": self.used_steps,
            "used_credits": self.used_credits,
            "remaining_steps": self.remaining_steps,
            "remaining_credits": self.remaining_credits,
        }


@dataclass(frozen=True, slots=True)
class TaskBudget:
    max_steps: int
    max_credits: float

    def __post_init__(self) -> None:
        if isinstance(self.max_steps, bool) or self.max_steps < 1:
            raise PlannerError("E04003", "task max_steps must be at least 1")
        if not math.isfinite(float(self.max_credits)) or self.max_credits <= 0:
            raise PlannerError("E04002", "task max_credits must be greater than 0")

    def to_dict(self) -> dict[str, int | float]:
        return {"max_steps": self.max_steps, "max_credits": self.max_credits}


def _clean_text(value: Any, field_name: str, *, max_length: int = 4000) -> str:
    value = str(value or "").strip()
    if not value or len(value) > max_length or "\x00" in value:
        raise PlannerError("E00001", f"{field_name} is invalid")
    return " ".join(value.split())


def _tuple_strings(value: Any, field_name: str, *, max_items: int = 100) -> tuple[str, ...]:
    if value is None:
        return ()
    if isinstance(value, (str, bytes)) or not isinstance(value, Iterable):
        raise PlannerError("E00001", f"{field_name} must be a list")
    values: list[str] = []
    for item in value:
        cleaned = _clean_text(item, field_name, max_length=512)
        if cleaned not in values:
            values.append(cleaned)
    if len(values) > max_items:
        raise PlannerError("E00001", f"{field_name} has too many items")
    return tuple(values)


@dataclass(frozen=True, slots=True)
class TaskSpec:
    id: str
    objective: str
    dependencies: tuple[str, ...] = ()
    executor: str = "default"
    estimated_steps: int = 1
    estimated_credits: float = 1.0
    context_refs: tuple[str, ...] = ()
    capabilities: tuple[str, ...] = ()
    idempotency_key: str | None = None
    depth: int = 0
    metadata: Mapping[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        object.__setattr__(self, "id", _clean_text(self.id, "task id", max_length=120))
        object.__setattr__(self, "objective", _clean_text(self.objective, "task objective"))
        object.__setattr__(self, "executor", _clean_text(self.executor, "executor", max_length=120))
        object.__setattr__(self, "dependencies", _tuple_strings(self.dependencies, "dependencies"))
        object.__setattr__(self, "context_refs", _tuple_strings(self.context_refs, "context_refs"))
        object.__setattr__(self, "capabilities", _tuple_strings(self.capabilities, "capabilities"))
        if isinstance(self.estimated_steps, bool) or self.estimated_steps < 1:
            raise PlannerError("E04003", "estimated_steps must be at least 1")
        if not math.isfinite(float(self.estimated_credits)) or self.estimated_credits <= 0:
            raise PlannerError("E04002", "estimated_credits must be greater than 0")
        if self.depth < 0:
            raise PlannerError("E07006", "task depth cannot be negative")
        if self.idempotency_key is not None:
            object.__setattr__(self, "idempotency_key", _clean_text(self.idempotency_key, "idempotency_key", max_length=256))
        if not isinstance(self.metadata, Mapping):
            raise PlannerError("E00001", "task metadata must be an object")

    @property
    def effective_idempotency_key(self) -> str:
        return self.idempotency_key or task_dedup_key(self)

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "objective": self.objective,
            "dependencies": list(self.dependencies),
            "executor": self.executor,
            "estimated_steps": self.estimated_steps,
            "estimated_credits": self.estimated_credits,
            "context_refs": list(self.context_refs),
            "capabilities": list(self.capabilities),
            "idempotency_key": self.effective_idempotency_key,
            "depth": self.depth,
            "metadata": dict(self.metadata),
            "status": TaskStatus.PENDING.value,
        }


def _task_canonical_payload(task: TaskSpec) -> dict[str, Any]:
    return {
        "objective": " ".join(task.objective.casefold().split()),
        "executor": task.executor.casefold(),
        "dependencies": sorted(task.dependencies),
        "context_refs": sorted(task.context_refs),
        "capabilities": sorted(task.capabilities),
    }


def task_dedup_key(task: TaskSpec) -> str:
    if task.idempotency_key:
        return f"idempotency:{task.idempotency_key}"
    encoded = json.dumps(_task_canonical_payload(task), sort_keys=True, separators=(",", ":"), ensure_ascii=True)
    return f"semantic:{hashlib.sha256(encoded.encode()).hexdigest()}"


@dataclass(frozen=True, slots=True)
class DeduplicationResult:
    tasks: tuple[TaskSpec, ...]
    aliases: Mapping[str, str]
    removed_ids: tuple[str, ...]


def deduplicate_tasks(tasks: Sequence[TaskSpec]) -> DeduplicationResult:
    ids: set[str] = set()
    canonical_ids: dict[str, str] = {}
    aliases: dict[str, str] = {}
    removed: list[str] = []
    unique: list[TaskSpec] = []
    for task in tasks:
        if task.id in ids:
            raise PlannerError("E07006", f"duplicate task id: {task.id}")
        ids.add(task.id)
        key = task_dedup_key(task)
        existing_id = canonical_ids.get(key)
        if existing_id:
            aliases[task.id] = existing_id
            removed.append(task.id)
            continue
        canonical_ids[key] = task.id
        unique.append(task)

    rewritten = tuple(
        replace(task, dependencies=tuple(aliases.get(dep, dep) for dep in task.dependencies))
        for task in unique
    )
    return DeduplicationResult(rewritten, dict(aliases), tuple(removed))


def _coerce_task(value: TaskSpec | Mapping[str, Any], index: int) -> TaskSpec:
    if isinstance(value, TaskSpec):
        return value
    if not isinstance(value, Mapping):
        raise PlannerError("E00001", "task proposal must be an object")
    task_id = value.get("id", value.get("task_id", f"task_{index + 1}"))
    objective = value.get("objective", value.get("description", value.get("title")))
    return TaskSpec(
        id=str(task_id),
        objective=str(objective or ""),
        dependencies=tuple(value.get("dependencies", value.get("depends_on", ())) or ()),
        executor=str(value.get("executor", value.get("role", "default"))),
        estimated_steps=int(value.get("estimated_steps", value.get("max_steps", 1))),
        estimated_credits=float(value.get("estimated_credits", value.get("max_credits", 1.0))),
        context_refs=tuple(value.get("context_refs", value.get("context", ())) or ()),
        capabilities=tuple(value.get("capabilities", ()) or ()),
        idempotency_key=value.get("idempotency_key"),
        depth=int(value.get("depth", 0)),
        metadata=value.get("metadata", {}),
    )


def _cycle_check(tasks: Sequence[TaskSpec]) -> None:
    by_id = {task.id: task for task in tasks}
    visiting: set[str] = set()
    visited: set[str] = set()

    def visit(task_id: str) -> None:
        if task_id in visiting:
            raise PlannerError("E07006", "task graph contains a cycle")
        if task_id in visited:
            return
        visiting.add(task_id)
        for dependency in by_id[task_id].dependencies:
            if dependency not in by_id:
                raise PlannerError("E00004", f"task dependency not found: {dependency}")
            visit(dependency)
        visiting.remove(task_id)
        visited.add(task_id)

    for task in tasks:
        visit(task.id)


def validate_plan(tasks: Sequence[TaskSpec], limits: PlannerLimits) -> tuple[TaskSpec, ...]:
    if not tasks:
        raise PlannerError("E00001", "plan must contain at least one task")
    if len(tasks) > limits.max_agents:
        raise PlannerError("E07006", f"plan contains more than {limits.max_agents} tasks")
    ids = [task.id for task in tasks]
    if len(ids) != len(set(ids)):
        raise PlannerError("E07006", "plan contains duplicate task ids")
    estimated_steps = sum(task.estimated_steps for task in tasks)
    estimated_credits = sum(task.estimated_credits for task in tasks)
    if estimated_steps > limits.max_steps:
        raise PlannerError("E04003", "plan estimated steps exceed the configured limit")
    if estimated_credits > limits.max_credits:
        raise PlannerError("E04002", "plan estimated credits exceed the configured limit")
    if any(task.depth > limits.max_depth for task in tasks):
        raise PlannerError("E07006", "plan task depth exceeds the configured limit")
    _cycle_check(tasks)
    return tuple(tasks)


@dataclass(frozen=True, slots=True)
class TaskPlan:
    id: str
    objective: str
    tasks: tuple[TaskSpec, ...]
    limits: PlannerLimits
    aliases: Mapping[str, str] = field(default_factory=dict)
    version: int = 1

    def __post_init__(self) -> None:
        object.__setattr__(self, "id", _clean_text(self.id, "plan id", max_length=120))
        object.__setattr__(self, "objective", _clean_text(self.objective, "plan objective"))
        validate_plan(self.tasks, self.limits)
        if self.version < 1:
            raise PlannerError("E00001", "plan version must be positive")

    def budget_for(self, task_id: str) -> TaskBudget:
        for task in self.tasks:
            if task.id == task_id:
                return TaskBudget(task.estimated_steps, task.estimated_credits)
        raise PlannerError("E00004", f"task not found: {task_id}")

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "objective": self.objective,
            "version": self.version,
            "limits": {
                "max_steps": self.limits.max_steps,
                "max_credits": self.limits.max_credits,
                "max_depth": self.limits.max_depth,
                "max_concurrency": self.limits.max_concurrency,
                "max_agents": self.limits.max_agents,
            },
            "tasks": [task.to_dict() for task in self.tasks],
            "aliases": dict(self.aliases),
        }


def decompose_tasks(
    objective: str,
    proposals: Sequence[TaskSpec | Mapping[str, Any]],
    *,
    limits: PlannerLimits | None = None,
    plan_id: str | None = None,
) -> TaskPlan:
    limits = limits or PlannerLimits()
    clean_objective = _clean_text(objective, "plan objective")
    if not proposals:
        raise PlannerError("E00001", "task proposals cannot be empty")
    tasks = tuple(_coerce_task(value, index) for index, value in enumerate(proposals))
    deduplicated = deduplicate_tasks(tasks)
    validate_plan(deduplicated.tasks, limits)
    if plan_id is None:
        material = json.dumps(
            {"objective": clean_objective, "tasks": [task_dedup_key(task) for task in deduplicated.tasks]},
            sort_keys=True,
            separators=(",", ":"),
        )
        plan_id = f"plan_{hashlib.sha256(material.encode()).hexdigest()[:20]}"
    return TaskPlan(plan_id, clean_objective, deduplicated.tasks, limits, deduplicated.aliases)


def allocate_task_budgets(tasks: Sequence[TaskSpec], budget: Budget) -> dict[str, TaskBudget]:
    validate_plan(tasks, PlannerLimits(max_steps=budget.max_steps, max_credits=budget.max_credits, max_agents=max(len(tasks), 1)))
    total_steps = sum(task.estimated_steps for task in tasks)
    total_credits = sum(task.estimated_credits for task in tasks)
    if total_steps > budget.remaining_steps:
        raise PlannerError("E04003", "child task step estimates exceed the remaining parent budget")
    if total_credits > budget.remaining_credits:
        raise PlannerError("E04002", "child task credit estimates exceed the remaining parent budget")
    return {
        task.id: TaskBudget(task.estimated_steps, task.estimated_credits)
        for task in tasks
    }


def _state_status(value: Any) -> str:
    status = getattr(value, "status", value)
    return str(getattr(status, "value", status))


def ready_task_ids(plan: TaskPlan, states: Mapping[str, Any]) -> tuple[str, ...]:
    ready: list[str] = []
    for task in plan.tasks:
        current = _state_status(states.get(task.id, TaskStatus.PENDING))
        if current != TaskStatus.PENDING.value:
            continue
        if all(_state_status(states.get(dependency, TaskStatus.PENDING)) == TaskStatus.SUCCEEDED.value for dependency in task.dependencies):
            ready.append(task.id)
    return tuple(ready)


def blocked_task_ids(plan: TaskPlan, states: Mapping[str, Any]) -> tuple[str, ...]:
    blocked: list[str] = []
    failed_statuses = {
        TaskStatus.FAILED.value,
        TaskStatus.BLOCKED.value,
        TaskStatus.CANCELLED.value,
    }
    for task in plan.tasks:
        if _state_status(states.get(task.id, TaskStatus.PENDING)) != TaskStatus.PENDING.value:
            continue
        if any(_state_status(states.get(dependency, TaskStatus.PENDING)) in failed_statuses for dependency in task.dependencies):
            blocked.append(task.id)
    return tuple(blocked)


def progress_fingerprint(states: Mapping[str, Any]) -> str:
    material: list[dict[str, Any]] = []
    for task_id in sorted(states):
        value = states[task_id]
        result = getattr(value, "result", None)
        marker = getattr(value, "progress_marker", None)
        if marker is None and result is not None:
            marker = getattr(result, "progress_marker", None)
        material.append({"id": task_id, "status": _state_status(value), "marker": marker})
    encoded = json.dumps(material, sort_keys=True, separators=(",", ":"), ensure_ascii=True, default=str)
    return hashlib.sha256(encoded.encode()).hexdigest()


def no_progress_detected(history: Sequence[str], *, window: int = 3) -> bool:
    if window < 1:
        raise PlannerError("E07006", "no-progress window must be positive")
    return len(history) >= window and len(set(history[-window:])) == 1


@dataclass(frozen=True, slots=True)
class ConvergenceDecision:
    should_stop: bool
    reason: ConvergenceReason
    message: str
    completed: int
    remaining: int

    def to_dict(self) -> dict[str, Any]:
        return {
            "should_stop": self.should_stop,
            "reason": self.reason.value,
            "message": self.message,
            "completed": self.completed,
            "remaining": self.remaining,
        }


def convergence_decision(
    plan: TaskPlan,
    states: Mapping[str, Any],
    budget: Budget,
    progress_history: Sequence[str] = (),
) -> ConvergenceDecision:
    statuses = {task.id: _state_status(states.get(task.id, TaskStatus.PENDING)) for task in plan.tasks}
    completed = sum(value == TaskStatus.SUCCEEDED.value for value in statuses.values())
    remaining = len(plan.tasks) - completed
    if completed == len(plan.tasks):
        return ConvergenceDecision(True, ConvergenceReason.COMPLETE, "all planned tasks succeeded", completed, remaining)
    if budget.exhausted:
        reason = ConvergenceReason.MAX_STEPS if budget.remaining_steps <= 0 else ConvergenceReason.BUDGET_EXHAUSTED
        return ConvergenceDecision(True, reason, "parent budget is exhausted", completed, remaining)
    if no_progress_detected(progress_history, window=plan.limits.no_progress_rounds):
        return ConvergenceDecision(True, ConvergenceReason.NO_PROGRESS, "task progress did not change for the configured window", completed, remaining)
    if any(value == TaskStatus.RUNNING.value for value in statuses.values()):
        return ConvergenceDecision(False, ConvergenceReason.CONTINUE, "tasks are still running", completed, remaining)
    if ready_task_ids(plan, statuses):
        return ConvergenceDecision(False, ConvergenceReason.CONTINUE, "ready tasks remain", completed, remaining)
    if any(value == TaskStatus.PENDING.value for value in statuses.values()):
        return ConvergenceDecision(True, ConvergenceReason.BLOCKED, "pending tasks have no satisfiable dependencies", completed, remaining)
    if any(value in {TaskStatus.FAILED.value, TaskStatus.CANCELLED.value, TaskStatus.BLOCKED.value} for value in statuses.values()):
        return ConvergenceDecision(True, ConvergenceReason.FAILED, "one or more tasks did not complete", completed, remaining)
    return ConvergenceDecision(False, ConvergenceReason.CONTINUE, "task execution may continue", completed, remaining)
