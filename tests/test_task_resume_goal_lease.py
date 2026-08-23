"""ADR-046: direct task resume inherits per-goal run ownership."""

from __future__ import annotations

import threading
import time
from pathlib import Path

from arion.state.models import PlanStep, TaskStatus, VerificationPolicy
from tests.test_goal_run_lease import (
    _AppendOnce,
    _BlockingPlanner,
    _close,
    _engine,
)


class _BlockingAppend(_AppendOnce):
    def __init__(self, effects: Path) -> None:
        super().__init__(effects)
        self.started = threading.Event()
        self.release = threading.Event()

    def execute(self, action, params):
        self.started.set()
        self.release.wait(timeout=5)
        return super().execute(action, params)


def _step() -> PlanStep:
    return PlanStep(
        index=0,
        intent="append exactly once",
        capability="goal.append",
        action="append",
        scope="goal:write",
        params={"path": "same.txt"},
        verification=VerificationPolicy("non_empty"),
        max_attempts=1,
    )


def _task(engine, storage, goal):
    task = engine.create_task(goal)
    task.steps = [_step()]
    storage.save_task(task)
    return task


def test_concurrent_distinct_same_goal_resumes_have_one_effect(
    tmp_path: Path,
) -> None:
    db = tmp_path / "direct-resume.db"
    effects = tmp_path / "direct-effects.log"
    capability = _BlockingAppend(effects)
    left, left_manager, left_store, left_cognition = _engine(
        db, _BlockingPlanner(forbidden=True), capability, lease=0.12
    )
    right, right_manager, right_store, right_cognition = _engine(
        db, _BlockingPlanner(forbidden=True), capability, lease=0.12
    )
    goal = left.submit_goal("append exactly once")
    first = _task(left, left_store, goal)
    second = _task(right, right_store, goal)
    owner_result: dict[str, object] = {}
    owner_errors: list[BaseException] = []

    def run_owner() -> None:
        try:
            owner_result["task"] = left.run_task(first.id)
        except BaseException as exc:
            owner_errors.append(exc)

    worker = threading.Thread(target=run_owner)
    worker.start()
    assert capability.started.wait(timeout=3)
    time.sleep(0.3)  # direct task ownership renews past its original lease

    contended = right.run_task(second.id)
    assert contended.status is TaskStatus.CREATED
    assert capability.calls == 0

    capability.release.set()
    worker.join(timeout=10)
    assert not owner_errors
    assert owner_result["task"].status is TaskStatus.COMPLETED
    assert capability.calls == 1
    assert effects.read_text(encoding="utf-8").splitlines() == ["effect-1"]
    assert right_store.load_task(second.id).status is TaskStatus.CREATED
    assert left_manager.get_goal(goal.id).status.value == "active"
    _close(left, left_store, left_cognition)
    _close(right, right_store, right_cognition)


def test_bulk_tasks_admits_one_requested_task_per_goal(tmp_path: Path) -> None:
    db = tmp_path / "bulk-resume.db"
    effects = tmp_path / "bulk-effects.log"
    capability = _AppendOnce(effects)
    engine, manager, storage, cognition = _engine(
        db, _BlockingPlanner(forbidden=True), capability
    )
    goal = engine.submit_goal("append exactly once")
    first = _task(engine, storage, goal)
    second = _task(engine, storage, goal)

    results = engine.run_tasks([first.id, second.id])

    assert results[first.id].status is TaskStatus.COMPLETED
    assert results[second.id].status is TaskStatus.CREATED
    assert capability.calls == 1
    assert effects.read_text(encoding="utf-8").splitlines() == ["effect-1"]
    _close(engine, storage, cognition)


def test_goal_loop_uses_owned_task_path_without_self_contention(
    tmp_path: Path,
) -> None:
    db = tmp_path / "nested-owned.db"
    effects = tmp_path / "nested-effects.log"
    capability = _AppendOnce(effects)
    engine, manager, storage, cognition = _engine(
        db, _BlockingPlanner(), capability
    )
    goal_id = engine.submit_goal("append exactly once").id

    result = engine.run_goal(goal_id)

    assert result.status.value == "completed"
    assert capability.calls == 1
    assert len([task for task in storage.list_tasks()
                if task.goal_id == goal_id]) == 1
    _close(engine, storage, cognition)
