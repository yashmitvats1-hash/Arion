"""ADR-047: durable PAUSED goal state stops new capability execution."""

from __future__ import annotations

import threading
from pathlib import Path

from arion.state.models import PlanStep, StepStatus, TaskStatus, VerificationPolicy
from arion.state.store import SQLiteStorage
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


def _step(index: int = 0) -> PlanStep:
    return PlanStep(
        index=index,
        intent=f"append {index}",
        capability="goal.append",
        action="append",
        scope="goal:write",
        params={"path": "same.txt"},
        verification=VerificationPolicy("non_empty"),
        max_attempts=1,
    )


def test_paused_goal_blocks_direct_task_resume_until_resumed(
    tmp_path: Path,
) -> None:
    db = tmp_path / "paused-resume.db"
    effects = tmp_path / "paused-resume.log"
    capability = _AppendOnce(effects)
    engine, manager, storage, cognition = _engine(
        db, _BlockingPlanner(forbidden=True), capability
    )
    goal = engine.submit_goal("append exactly once")
    task = engine.create_task(goal)
    task.steps = [_step()]
    storage.save_task(task)
    manager.pause(goal.id, reason="operator pause")

    paused = engine.run_task(task.id)

    assert paused.status is TaskStatus.CREATED
    assert paused.steps[0].status is StepStatus.PENDING
    assert capability.calls == 0
    assert not effects.exists()

    manager.resume(goal.id, reason="operator resume")
    completed = engine.run_task(task.id)
    assert completed.status is TaskStatus.COMPLETED
    assert capability.calls == 1
    assert effects.read_text(encoding="utf-8").splitlines() == ["effect-1"]
    _close(engine, storage, cognition)


def test_pause_committed_during_planning_stops_before_capability(
    tmp_path: Path,
) -> None:
    db = tmp_path / "pause-planning.db"
    effects = tmp_path / "pause-planning.log"
    capability = _AppendOnce(effects)
    planner = _BlockingPlanner(block=True)
    engine, manager, storage, cognition = _engine(db, planner, capability)
    goal_id = engine.submit_goal("append exactly once").id
    result: dict[str, object] = {}
    errors: list[BaseException] = []

    def run() -> None:
        try:
            result["goal"] = engine.run_goal(goal_id)
        except BaseException as exc:
            errors.append(exc)

    worker = threading.Thread(target=run)
    worker.start()
    assert planner.started.wait(timeout=3)
    manager.pause(goal_id, reason="operator pause during planning")
    planner.release.set()
    worker.join(timeout=10)

    assert not errors
    assert result["goal"].status.value == "paused"
    task = next(task for task in storage.list_tasks()
                if task.goal_id == goal_id)
    assert task.status is TaskStatus.CREATED
    assert task.steps == []
    assert capability.calls == 0
    assert not effects.exists()

    manager.resume(goal_id, reason="operator resume")
    completed_goal = engine.run_goal(goal_id)
    assert completed_goal.status.value == "completed"
    assert planner.calls == 2
    assert capability.calls == 1
    _close(engine, storage, cognition)


def test_pause_during_inflight_step_persists_result_and_stops_next(
    tmp_path: Path,
) -> None:
    db = tmp_path / "pause-inflight.db"
    effects = tmp_path / "pause-inflight.log"
    capability = _BlockingAppend(effects)
    engine, manager, storage, cognition = _engine(
        db, _BlockingPlanner(forbidden=True), capability
    )
    goal = engine.submit_goal("append twice")
    task = engine.create_task(goal)
    task.steps = [_step(0), _step(1)]
    storage.save_task(task)
    result: dict[str, object] = {}
    errors: list[BaseException] = []

    def run() -> None:
        try:
            result["task"] = engine.run_task(task.id)
        except BaseException as exc:
            errors.append(exc)

    worker = threading.Thread(target=run)
    worker.start()
    assert capability.started.wait(timeout=3)
    manager.pause(goal.id, reason="pause while first effect is in flight")
    capability.release.set()
    worker.join(timeout=10)

    assert not errors
    paused_task = storage.load_task(task.id)
    assert result["task"].status is TaskStatus.RUNNING
    assert paused_task.status is TaskStatus.RUNNING
    assert paused_task.steps[0].status is StepStatus.SUCCEEDED
    assert paused_task.steps[1].status is StepStatus.PENDING
    assert capability.calls == 1

    manager.resume(goal.id, reason="resume remaining work")
    completed = engine.run_task(task.id)
    assert completed.status is TaskStatus.COMPLETED
    assert [step.status for step in completed.steps] == [
        StepStatus.SUCCEEDED, StepStatus.SUCCEEDED,
    ]
    assert capability.calls == 2
    assert effects.read_text(encoding="utf-8").splitlines() == [
        "effect-1", "effect-2",
    ]
    _close(engine, storage, cognition)


def test_pause_after_lock_wait_releases_without_mutation(
    tmp_path: Path,
) -> None:
    from tests.test_lock_waiting import (
        FS,
        FakeTime,
        _approve,
        _engine as waiting_engine,
        _sandbox,
    )

    sandbox = _sandbox(tmp_path)
    db = tmp_path / "pause-lock-wait.db"
    clock = FakeTime()
    holder = SQLiteStorage(db)
    held = holder.acquire(
        FS, "notes.txt", "filesystem.write", "write", "holder",
        lease_seconds=3600, now=clock.now(),
    )

    class ReleaseHolder:
        def __init__(self) -> None:
            self.done = False

        def sleep(self, seconds: float) -> None:
            if not self.done:
                self.done = True
                holder.release(held.lock_id, "holder")
            clock.sleep(seconds)

    sleeper = ReleaseHolder()
    engine, manager, storage, registry = waiting_engine(
        db, sandbox, max_wait=60,
        backoff_base=0.1, backoff_max=0.1,
        clock=clock.now, sleeper=sleeper,
    )
    goal_id = engine.submit_goal("write notes").id
    _approve(engine, goal_id)
    original_clear = manager.clear_blocker

    def clear_then_pause(target_goal_id, blocker_key, reason="resolved"):
        cleared = original_clear(target_goal_id, blocker_key, reason)
        if blocker_key == "lock_contention":
            manager.pause(target_goal_id, reason="pause after lock wait")
        return cleared

    manager.clear_blocker = clear_then_pause
    result = engine.run_goal(goal_id)

    assert result.status.value == "paused"
    assert registry.get("filesystem.write").calls == 0
    task = manager.task_history(goal_id)[-1]
    assert task.steps[0].status is StepStatus.PENDING
    assert task.lock_wait is None
    assert storage.list(resource_kind=FS, resource="notes.txt") == []
    engine.shutdown(); storage.close(); holder.close()
