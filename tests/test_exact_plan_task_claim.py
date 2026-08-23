"""ADR-051: exact plan-version task publication is create-or-adopt."""

from __future__ import annotations

import threading
import time
from pathlib import Path

import pytest

from arion.state.models import PlanStep, TaskStatus, VerificationPolicy
from tests.test_goal_run_lease import _AppendOnce, _BlockingPlanner, _close, _engine


def _step(path: str = "same.txt") -> PlanStep:
    return PlanStep(
        index=0,
        intent="append once",
        capability="goal.append",
        action="append",
        scope="goal:write",
        params={"path": path},
        verification=VerificationPolicy("non_empty"),
        max_attempts=1,
    )


def test_stale_owner_concurrent_reconstruction_adopts_one_task(
    tmp_path: Path,
) -> None:
    db = tmp_path / "reconstruct-race.db"
    effects = tmp_path / "reconstruct-effects.log"
    capability = _AppendOnce(effects)
    first, first_manager, first_storage, first_cognition = _engine(
        db, _BlockingPlanner(forbidden=True), capability, lease=0.12
    )
    second, second_manager, second_storage, second_cognition = _engine(
        db, _BlockingPlanner(forbidden=True), capability, lease=0.12
    )
    goal = first.submit_goal("append exactly once")
    step = _step()
    plan = first_manager.record_plan_version(
        goal.id, "direct", [step.to_dict()], reason="initial_plan"
    )

    stale_claim = first._acquire_goal_run_lease(goal.id)
    checked = threading.Event()
    proceed = threading.Event()
    original_history = first_manager.task_history

    def stale_task_history(goal_id):
        observed = original_history(goal_id)
        checked.set()
        proceed.wait(timeout=5)
        return observed

    # The task-history observation may become stale; the store claim remains
    # authoritative after this point.
    first_manager.task_history = stale_task_history
    reconstructed: dict[str, object] = {}
    errors: list[BaseException] = []

    def reconstruct_first() -> None:
        try:
            reconstructed["first"] = first._plan_for_goal(goal.id)
        except BaseException as exc:
            errors.append(exc)

    worker = threading.Thread(target=reconstruct_first)
    worker.start()
    assert checked.wait(timeout=3)
    first._stop_lock_heartbeat(stale_claim[1])
    time.sleep(0.18)

    current_claim = second._acquire_goal_run_lease(goal.id)
    reconstructed["second"] = second._plan_for_goal(goal.id)
    proceed.set()
    worker.join(timeout=5)
    first_manager.task_history = original_history
    second._release_goal_run_lease(goal.id, current_claim)
    first._release_goal_run_lease(goal.id, (stale_claim[0], None))

    assert not errors
    assert reconstructed["first"].id == reconstructed["second"].id
    exact = [
        task for task in first_storage.list_tasks()
        if task.goal_id == goal.id
        and task.plan_version == plan["plan_version"]
    ]
    assert len(exact) == 1
    assert exact[0].status is TaskStatus.PLANNED

    first_result = first.run_task(exact[0].id)
    second_result = second.run_task(exact[0].id)
    assert first_result.status is TaskStatus.COMPLETED
    assert second_result.status is TaskStatus.COMPLETED
    assert capability.calls == 1
    assert effects.read_text(encoding="utf-8").splitlines() == ["effect-1"]
    _close(first, first_storage, first_cognition)
    _close(second, second_storage, second_cognition)


def test_repeated_reconstruction_returns_existing_without_duplicate_events(
    tmp_path: Path,
) -> None:
    db = tmp_path / "repeat-reconstruct.db"
    capability = _AppendOnce(tmp_path / "unused.log")
    engine, manager, storage, cognition = _engine(
        db, _BlockingPlanner(forbidden=True), capability
    )
    goal = engine.submit_goal("repeat reconstruction")
    step = _step()
    manager.record_plan_version(
        goal.id, "direct", [step.to_dict()], reason="initial_plan"
    )

    first = engine._plan_for_goal(goal.id)
    first_event_count = len([
        event for event in storage.list_events(first.id)
        if event.kind in ("task.created", "plan.produced")
    ])
    second = engine._plan_for_goal(goal.id)

    assert first.id == second.id
    assert len([task for task in storage.list_tasks()
                if task.goal_id == goal.id and task.plan_version == 1]) == 1
    assert first_event_count == 2
    assert len([
        event for event in storage.list_events(first.id)
        if event.kind in ("task.created", "plan.produced")
    ]) == first_event_count
    _close(engine, storage, cognition)


def test_task_created_before_event_failure_is_adopted_on_retry(
    tmp_path: Path,
) -> None:
    db = tmp_path / "event-gap.db"
    capability = _AppendOnce(tmp_path / "event-effects.log")
    engine, manager, storage, cognition = _engine(
        db, _BlockingPlanner(forbidden=True), capability
    )
    goal = engine.submit_goal("event gap reconstruction")
    manager.record_plan_version(
        goal.id, "direct", [_step().to_dict()], reason="initial_plan"
    )
    original_emit = engine._emit

    def fail_task_created(kind, *args, **kwargs):
        if kind == "task.created":
            raise RuntimeError("event unavailable after task commit")
        return original_emit(kind, *args, **kwargs)

    engine._emit = fail_task_created
    with pytest.raises(RuntimeError, match="event unavailable"):
        engine._plan_for_goal(goal.id)
    engine._emit = original_emit

    exact_before = [task for task in storage.list_tasks()
                    if task.goal_id == goal.id and task.plan_version == 1]
    assert len(exact_before) == 1
    adopted = engine._plan_for_goal(goal.id)
    assert adopted.id == exact_before[0].id
    assert len([task for task in storage.list_tasks()
                if task.goal_id == goal.id and task.plan_version == 1]) == 1
    assert capability.calls == 0
    _close(engine, storage, cognition)


def test_legacy_duplicate_exact_tasks_execute_only_canonical(
    tmp_path: Path,
) -> None:
    db = tmp_path / "legacy-duplicates.db"
    effects = tmp_path / "legacy-duplicate-effects.log"
    capability = _AppendOnce(effects)
    engine, manager, storage, cognition = _engine(
        db, _BlockingPlanner(forbidden=True), capability
    )
    goal = engine.submit_goal("canonical exact task")
    step = _step()
    manager.record_plan_version(
        goal.id, "direct", [step.to_dict()], reason="initial_plan"
    )

    first = engine.create_task(goal, plan_version=1)
    first.steps = [_step()]
    first.status = TaskStatus.PLANNED
    storage.save_task(first)
    second = engine.create_task(goal, plan_version=1)
    second.steps = [_step()]
    second.status = TaskStatus.PLANNED
    storage.save_task(second)

    rejected = engine.run_task(second.id)
    completed = engine.run_task(first.id)

    assert rejected.status is TaskStatus.FAILED
    assert "superseded" in (rejected.error or "")
    assert completed.status is TaskStatus.COMPLETED
    assert capability.calls == 1
    assert effects.read_text(encoding="utf-8").splitlines() == ["effect-1"]
    _close(engine, storage, cognition)


def test_divergent_exact_task_is_not_adopted_as_reconstruction(
    tmp_path: Path,
) -> None:
    db = tmp_path / "divergent-task.db"
    effects = tmp_path / "divergent-effects.log"
    capability = _AppendOnce(effects)
    engine, manager, storage, cognition = _engine(
        db, _BlockingPlanner(forbidden=True), capability
    )
    goal = engine.submit_goal("immutable reconstruction")
    manager.record_plan_version(
        goal.id, "direct", [_step("expected.txt").to_dict()],
        reason="initial_plan",
    )
    task = engine.create_task(goal, plan_version=1)
    task.steps = [_step("forged.txt")]
    task.status = TaskStatus.PLANNED
    storage.save_task(task)

    with pytest.raises(ValueError, match="diverges from stored plan"):
        engine._plan_for_goal(goal.id)

    # Existing task mutation remains a separate compatibility/authorization
    # concern; reconstruction itself never adopts the divergent definition.
    durable = storage.load_task(task.id)
    assert durable.status is TaskStatus.PLANNED
    assert durable.steps[0].params == {"path": "forged.txt"}
    assert capability.calls == 0
    assert not effects.exists()
    _close(engine, storage, cognition)
