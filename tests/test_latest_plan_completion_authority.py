"""ADR-048: only latest-plan task steps authorize goal completion."""

from __future__ import annotations

from pathlib import Path

from arion.state.models import PlanStep, StepStatus, Task, TaskStatus, VerificationPolicy
from tests.test_goal_run_lease import _AppendOnce, _BlockingPlanner, _close, _engine


def _step(index: int, *, status: StepStatus = StepStatus.PENDING) -> PlanStep:
    result = {"historical": True} if status is StepStatus.SUCCEEDED else None
    return PlanStep(
        index=index,
        intent=f"step {index}",
        capability="goal.append",
        action="append",
        scope="goal:write",
        params={"path": f"file-{index}.txt"},
        verification=VerificationPolicy("non_empty"),
        max_attempts=1,
        status=status,
        result=result,
    )


def _save_task(storage, goal, plan_version, steps, status):
    task = Task(
        id=f"task-{plan_version}-{len(storage.list_tasks())}",
        goal_id=goal.id,
        description=goal.description,
        plan_version=plan_version,
        steps=steps,
        status=status,
    )
    storage.save_task(task)
    return task


def test_newer_plan_without_task_cannot_complete_from_old_success(
    tmp_path: Path,
) -> None:
    db = tmp_path / "plan-before-task.db"
    effects = tmp_path / "latest-effects.log"
    capability = _AppendOnce(effects)
    engine, manager, storage, cognition = _engine(
        db, _BlockingPlanner(forbidden=True), capability
    )
    goal = engine.submit_goal("execute latest plan")

    old_step = _step(0, status=StepStatus.SUCCEEDED)
    version1 = manager.record_plan_version(
        goal.id, "direct", [old_step.to_dict()], reason="initial_plan"
    )
    _save_task(
        storage, goal, version1["plan_version"], [old_step],
        TaskStatus.COMPLETED,
    )
    new_step = _step(0)
    version2 = manager.record_plan_version(
        goal.id, "direct", [new_step.to_dict()],
        reason="replan_new_requirement",
    )

    evaluation, _ = manager.evaluate(goal.id)
    assert evaluation.next_action == "continue"
    assert evaluation.evidence["reason"] == "outstanding_work"
    assert evaluation.evidence["latest_plan_tasks"] == 0
    assert evaluation.evidence["latest_handled_steps"] == 0

    result = engine.run_goal(goal.id)
    assert result.status.value == "completed"
    latest_tasks = [
        task for task in storage.list_tasks()
        if task.goal_id == goal.id
        and task.plan_version == version2["plan_version"]
    ]
    assert len(latest_tasks) == 1
    assert latest_tasks[0].status is TaskStatus.COMPLETED
    assert capability.calls == 1
    assert effects.read_text(encoding="utf-8").splitlines() == ["effect-1"]
    _close(engine, storage, cognition)


def test_historical_successes_do_not_inflate_partial_latest_plan(
    tmp_path: Path,
) -> None:
    db = tmp_path / "partial-latest.db"
    capability = _AppendOnce(tmp_path / "unused.log")
    engine, manager, storage, cognition = _engine(
        db, _BlockingPlanner(forbidden=True), capability
    )
    goal = engine.submit_goal("partial latest plan")

    old_steps = [_step(index, status=StepStatus.SUCCEEDED) for index in range(4)]
    version1 = manager.record_plan_version(
        goal.id, "direct", [step.to_dict() for step in old_steps],
        reason="initial_plan",
    )
    _save_task(storage, goal, version1["plan_version"], old_steps,
               TaskStatus.COMPLETED)

    latest_steps = [_step(0, status=StepStatus.SUCCEEDED), _step(1)]
    version2 = manager.record_plan_version(
        goal.id, "direct", [step.to_dict() for step in latest_steps],
        reason="replan_partial",
    )
    _save_task(storage, goal, version2["plan_version"], latest_steps,
               TaskStatus.RUNNING)

    evaluation, _ = manager.evaluate(goal.id)
    assert evaluation.next_action == "continue"
    assert evaluation.evidence["reason"] == "resume_pending"
    assert evaluation.evidence["succeeded_steps"] == 5  # observational history
    assert evaluation.evidence["latest_succeeded_steps"] == 1
    assert evaluation.evidence["latest_handled_steps"] == 1
    _close(engine, storage, cognition)


def test_duplicate_latest_step_indices_cannot_satisfy_distinct_steps(
    tmp_path: Path,
) -> None:
    db = tmp_path / "duplicate-indices.db"
    capability = _AppendOnce(tmp_path / "unused-duplicate.log")
    engine, manager, storage, cognition = _engine(
        db, _BlockingPlanner(forbidden=True), capability
    )
    goal = engine.submit_goal("distinct latest indices")
    plan_steps = [_step(0), _step(1)]
    version = manager.record_plan_version(
        goal.id, "direct", [step.to_dict() for step in plan_steps],
        reason="initial_plan",
    )
    for _ in range(2):
        _save_task(
            storage, goal, version["plan_version"],
            [_step(0, status=StepStatus.SUCCEEDED)],
            TaskStatus.COMPLETED,
        )

    evaluation, _ = manager.evaluate(goal.id)
    assert evaluation.next_action == "continue"
    assert evaluation.evidence["latest_handled_steps"] == 1
    _close(engine, storage, cognition)


def test_unversioned_legacy_task_counts_when_no_exact_task_exists(
    tmp_path: Path,
) -> None:
    db = tmp_path / "legacy-unversioned.db"
    capability = _AppendOnce(tmp_path / "unused-legacy.log")
    engine, manager, storage, cognition = _engine(
        db, _BlockingPlanner(forbidden=True), capability
    )
    goal = engine.submit_goal("legacy completion")
    plan_step = _step(0)
    manager.record_plan_version(
        goal.id, "direct", [plan_step.to_dict()], reason="initial_plan"
    )
    legacy_step = _step(0, status=StepStatus.SUCCEEDED)
    _save_task(storage, goal, None, [legacy_step], TaskStatus.COMPLETED)

    evaluation, _ = manager.evaluate(goal.id)
    assert evaluation.next_action == "complete"
    assert evaluation.evidence["latest_plan_tasks"] == 1
    assert evaluation.evidence["latest_handled_steps"] == 1
    _close(engine, storage, cognition)
