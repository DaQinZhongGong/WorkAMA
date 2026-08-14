import pytest

from workama_agent.planner import (
    Budget,
    ConvergenceReason,
    PlannerError,
    PlannerLimits,
    TaskStatus,
    convergence_decision,
    decompose_tasks,
    no_progress_detected,
    progress_fingerprint,
    ready_task_ids,
    task_dedup_key,
)


def test_decompose_deduplicates_idempotent_work_and_rewrites_dependencies():
    plan = decompose_tasks(
        "Prepare a release summary",
        [
            {"id": "research_a", "objective": "Collect release facts", "idempotency_key": "facts-v1"},
            {"id": "research_duplicate", "objective": "Collect release facts", "idempotency_key": "facts-v1"},
            {"id": "summarize", "objective": "Write the summary", "dependencies": ["research_duplicate"]},
        ],
    )

    assert [task.id for task in plan.tasks] == ["research_a", "summarize"]
    assert plan.aliases == {"research_duplicate": "research_a"}
    assert plan.tasks[1].dependencies == ("research_a",)
    assert plan.to_dict()["tasks"][0]["idempotency_key"] == "facts-v1"


def test_semantic_dedup_key_is_stable_and_normalizes_objective_whitespace():
    left = decompose_tasks("Goal", [{"id": "a", "objective": "  Read   the docs  "}]).tasks[0]
    right = decompose_tasks(
        "Goal",
        [{"id": "b", "objective": "Read the docs", "estimated_steps": 3, "estimated_credits": 4}],
        limits=PlannerLimits(max_steps=5, max_credits=5),
    ).tasks[0]
    assert task_dedup_key(left) == task_dedup_key(right)


def test_plan_rejects_unknown_dependencies_and_cycles():
    with pytest.raises(PlannerError, match="dependency not found"):
        decompose_tasks("Goal", [{"id": "a", "objective": "A", "dependencies": ["missing"]}])

    with pytest.raises(PlannerError, match="contains a cycle"):
        decompose_tasks(
            "Goal",
            [
                {"id": "a", "objective": "A", "dependencies": ["b"]},
                {"id": "b", "objective": "B", "dependencies": ["a"]},
            ],
        )


def test_plan_enforces_step_credit_and_agent_limits():
    limits = PlannerLimits(max_steps=2, max_credits=2, max_agents=2)
    with pytest.raises(PlannerError, match="estimated steps"):
        decompose_tasks(
            "Goal",
            [{"id": "a", "objective": "A", "estimated_steps": 2}, {"id": "b", "objective": "B"}],
            limits=limits,
        )

    with pytest.raises(PlannerError, match="more than 2"):
        decompose_tasks(
            "Goal",
            [{"id": str(index), "objective": str(index)} for index in range(3)],
            limits=limits,
        )


def test_ready_tasks_only_include_satisfied_dependencies():
    plan = decompose_tasks(
        "Goal",
        [
            {"id": "a", "objective": "A"},
            {"id": "b", "objective": "B", "dependencies": ["a"]},
            {"id": "c", "objective": "C"},
        ],
    )
    assert ready_task_ids(plan, {}) == ("a", "c")
    assert ready_task_ids(plan, {"a": TaskStatus.SUCCEEDED}) == ("b", "c")


def test_convergence_detects_success_budget_and_no_progress():
    plan = decompose_tasks("Goal", [{"id": "a", "objective": "A"}])
    success = convergence_decision(plan, {"a": TaskStatus.SUCCEEDED}, Budget(5, 5))
    assert success.should_stop and success.reason == ConvergenceReason.COMPLETE

    exhausted = convergence_decision(plan, {"a": TaskStatus.PENDING}, Budget(1, 1, used_steps=1))
    assert exhausted.reason == ConvergenceReason.MAX_STEPS

    stalled = convergence_decision(plan, {"a": TaskStatus.RUNNING}, Budget(5, 5), ["same", "same", "same"])
    assert stalled.should_stop and stalled.reason == ConvergenceReason.NO_PROGRESS


def test_progress_fingerprint_and_window_are_deterministic():
    first = progress_fingerprint({"b": "pending", "a": "running"})
    second = progress_fingerprint({"a": "running", "b": "pending"})
    assert first == second
    assert no_progress_detected([first, first, first])
    assert not no_progress_detected([first, first])
