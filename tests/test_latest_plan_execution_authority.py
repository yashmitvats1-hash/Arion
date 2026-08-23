"""ADR-049: only current latest-plan tasks may execute or be approved."""

from __future__ import annotations

from pathlib import Path

import pytest

from arion.orchestration.authz import ApprovalOutcome
from arion.state.approvals import ApprovalError, ApprovalStatus
from arion.state.models import PlanStep, StepStatus, TaskStatus, VerificationPolicy
from arion.state.store import SQLiteStorage
from tests.test_goal_run_lease import _AppendOnce, _BlockingPlanner, _close, _engine
from tests.test_lock_waiting import (
    FS,
    FakeTime,
    _approve,
    _engine as waiting_engine,
    _sandbox,
)


def _append_step(path: str) -> PlanStep:
    return PlanStep(
        index=0,
        intent=f"append {path}",
        capability="goal.append",
        action="append",
        scope="goal:write",
        params={"path": path},
        verification=VerificationPolicy("non_empty"),
        max_attempts=1,
    )


def test_superseded_pending_approval_is_denied_and_cannot_execute(
    tmp_path: Path,
) -> None:
    sandbox = _sandbox(tmp_path)
    db = tmp_path / "superseded-approval.db"
    engine, manager, storage, registry = waiting_engine(
        db, sandbox, max_wait=0
    )
    goal_id = engine.submit_goal("write notes").id
    engine.run_goal(goal_id)
    old_task = manager.task_history(goal_id)[-1]
    old_request = engine.approval_store.list_requests(status="pending")[0]
    latest_step = PlanStep(
        index=0,
        intent="write newer target",
        capability="filesystem.write",
        action="write",
        scope="filesystem:write",
        params={"path": "newer.txt", "content": "new", "overwrite": False},
        verification=VerificationPolicy("write_verified"),
    )
    latest = manager.record_plan_version(
        goal_id, "direct", [latest_step.to_dict()],
        reason="replan_new_target",
    )

    with pytest.raises(ApprovalError, match="superseded plan task"):
        engine.resolve_approval_request(
            old_request.approval_id,
            ApprovalOutcome.APPROVED,
            actor="operator:alice",
        )

    denied = storage.get_request(old_request.approval_id)
    fenced = storage.load_task(old_task.id)
    assert denied.status is ApprovalStatus.DENIED
    assert denied.decision_actor == "system:superseded_plan"
    assert fenced.status is TaskStatus.FAILED
    assert "superseded" in (fenced.error or "")
    assert registry.get("filesystem.write").calls == 0
    assert not (sandbox / "notes.txt").exists()
    assert not (sandbox / "newer.txt").exists()

    # Current work is still recoverable through stored-plan reconstruction.
    current = engine.run_goal(goal_id)
    assert current.status.value == "blocked"  # fresh approval for version 2
    latest_tasks = [
        task for task in storage.list_tasks()
        if task.goal_id == goal_id
        and task.plan_version == latest["plan_version"]
    ]
    assert len(latest_tasks) == 1
    assert latest_tasks[0].status is TaskStatus.AWAITING_APPROVAL
    engine.shutdown(); storage.close()


def test_direct_superseded_task_resume_fails_without_effect(
    tmp_path: Path,
) -> None:
    db = tmp_path / "superseded-direct.db"
    effects = tmp_path / "superseded-direct.log"
    capability = _AppendOnce(effects)
    engine, manager, storage, cognition = _engine(
        db, _BlockingPlanner(forbidden=True), capability
    )
    goal = engine.submit_goal("execute current append")
    old_step = _append_step("old.txt")
    old_plan = manager.record_plan_version(
        goal.id, "direct", [old_step.to_dict()], reason="initial_plan"
    )
    old_task = engine.create_task(goal, plan_version=old_plan["plan_version"])
    old_task.steps = [old_step]
    storage.save_task(old_task)
    new_step = _append_step("new.txt")
    manager.record_plan_version(
        goal.id, "direct", [new_step.to_dict()], reason="replan_new_target"
    )

    result = engine.run_task(old_task.id)

    assert result.status is TaskStatus.FAILED
    assert result.steps[0].status is StepStatus.FAILED
    assert "superseded" in (result.error or "")
    assert capability.calls == 0
    assert not effects.exists()
    _close(engine, storage, cognition)


def test_new_plan_committed_during_lock_wait_fences_old_mutation(
    tmp_path: Path,
) -> None:
    sandbox = _sandbox(tmp_path)
    db = tmp_path / "superseded-lock-wait.db"
    clock = FakeTime()
    holder = SQLiteStorage(db)
    held = holder.acquire(
        FS, "notes.txt", "filesystem.write", "write", "holder",
        lease_seconds=3600, now=clock.now(),
    )

    class ReplanAndRelease:
        def __init__(self) -> None:
            self.manager = None
            self.goal_id = None
            self.done = False

        def sleep(self, seconds: float) -> None:
            if not self.done:
                self.done = True
                new_step = PlanStep(
                    index=0,
                    intent="write newer target",
                    capability="filesystem.write",
                    action="write",
                    scope="filesystem:write",
                    params={"path": "newer.txt", "content": "new", "overwrite": False},
                    verification=VerificationPolicy("write_verified"),
                )
                self.manager.record_plan_version(
                    self.goal_id, "direct", [new_step.to_dict()],
                    reason="replan_during_lock_wait",
                )
                holder.release(held.lock_id, "holder")
            clock.sleep(seconds)

    sleeper = ReplanAndRelease()
    engine, manager, storage, registry = waiting_engine(
        db, sandbox, max_wait=60,
        backoff_base=0.1, backoff_max=0.1,
        clock=clock.now, sleeper=sleeper,
    )
    goal_id = engine.submit_goal("write notes").id
    _approve(engine, goal_id)
    old_task = manager.task_history(goal_id)[-1]
    sleeper.manager = manager
    sleeper.goal_id = goal_id

    engine.run_goal(goal_id)

    fenced = storage.load_task(old_task.id)
    assert fenced.status is TaskStatus.FAILED
    assert "superseded" in (fenced.error or "")
    assert fenced.lock_wait is None
    assert registry.get("filesystem.write").calls == 0
    assert not (sandbox / "notes.txt").exists()
    assert not (sandbox / "newer.txt").exists()
    assert storage.list(resource_kind=FS, resource="notes.txt") == []
    engine.shutdown(); storage.close(); holder.close()


def test_exact_latest_and_legacy_fallback_tasks_still_execute(
    tmp_path: Path,
) -> None:
    db = tmp_path / "current-compatible.db"
    effects = tmp_path / "current-compatible.log"
    capability = _AppendOnce(effects)
    engine, manager, storage, cognition = _engine(
        db, _BlockingPlanner(forbidden=True), capability
    )

    exact_goal = engine.submit_goal("exact latest")
    exact_step = _append_step("exact.txt")
    exact_plan = manager.record_plan_version(
        exact_goal.id, "direct", [exact_step.to_dict()], reason="initial_plan"
    )
    exact_task = engine.create_task(
        exact_goal, plan_version=exact_plan["plan_version"]
    )
    exact_task.steps = [exact_step]
    storage.save_task(exact_task)
    assert engine.run_task(exact_task.id).status is TaskStatus.COMPLETED

    legacy_goal = engine.submit_goal("legacy latest")
    legacy_step = _append_step("legacy.txt")
    manager.record_plan_version(
        legacy_goal.id, "direct", [legacy_step.to_dict()], reason="initial_plan"
    )
    legacy_task = engine.create_task(legacy_goal)  # created after plan; no exact task
    legacy_task.steps = [legacy_step]
    storage.save_task(legacy_task)
    assert engine.run_task(legacy_task.id).status is TaskStatus.COMPLETED

    assert capability.calls == 2
    _close(engine, storage, cognition)
