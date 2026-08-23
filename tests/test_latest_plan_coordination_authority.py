"""ADR-053: coordination/progress authority follows the latest plan.

Superseded, historical, or noncanonical tasks keep their rows (historical
retention) but must not retain COORDINATION authority over the current goal:

  - a superseded AWAITING_APPROVAL task must not block latest-plan work;
  - a superseded mutation-lock waiter must not permanently stall progress;
  - a noncanonical exact-version duplicate must not influence progress,
    replan, or re-execution decisions.

Current-plan coordination (awaiting approval, lock waiting) keeps working
exactly as before.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from arion.orchestration.authz import ApprovalOutcome
from arion.state.approvals import ApprovalStatus
from arion.state.models import PlanStep, StepStatus, TaskStatus, VerificationPolicy
from arion.state.store import SQLiteStorage
from tests.test_lock_waiting import (
    FakeTime,
    InterruptSleeper,
    _approve,
    _engine,
    _hold_lock,
    _sandbox,
)


def _write_step(path: str) -> PlanStep:
    return PlanStep(
        index=0,
        intent=f"write {path}",
        capability="filesystem.write",
        action="write",
        scope="filesystem:write",
        params={"path": path, "content": "hello", "overwrite": False},
        verification=VerificationPolicy("write_verified"),
    )


# --------------------------------------------------------------------------- #
# Test 1 — superseded approval cannot block latest work
# --------------------------------------------------------------------------- #


def test_superseded_approval_does_not_block_latest_plan_work(tmp_path: Path) -> None:
    sandbox = _sandbox(tmp_path)
    db = tmp_path / "coord-approval.db"
    engine, manager, storage, registry = _engine(db, sandbox, max_wait=0)
    goal_id = engine.submit_goal("write notes").id
    engine.run_goal(goal_id)
    old_task = manager.task_history(goal_id)[-1]
    old_request = engine.approval_store.list_requests(status="pending")[0]
    assert old_task.plan_version == 1
    assert old_task.status is TaskStatus.AWAITING_APPROVAL

    # Newer immutable plan v2 through the legitimate lineage funnel (the same
    # funnel `arion goal rollback` uses) while the v1 approval is pending.
    latest = manager.record_plan_version(
        goal_id, "direct", [_write_step("newer.txt").to_dict()],
        reason="replan_new_target",
    )
    assert latest["plan_version"] == 2

    # Previously observed defect: blocked goal, ZERO v2 tasks, dead v1
    # approval still queued as the only pending request.
    goal = engine.run_goal(goal_id)

    # Historical rows are RETAINED, but the stale approval loses authority:
    # denied by the existing supersession fence (not deleted).
    old_request_after = storage.get_request(old_request.approval_id)
    old_task_after = storage.load_task(old_task.id)
    assert old_request_after is not None
    assert old_request_after.status is ApprovalStatus.DENIED
    assert old_request_after.decision_actor == "system:superseded_plan"
    assert old_task_after is not None
    assert old_task_after.status is TaskStatus.FAILED
    assert "superseded" in (old_task_after.error or "")

    # Latest-plan work was published through stored-plan reconstruction and
    # proceeds to its OWN fresh approval; only current work is queued.
    latest_tasks = [
        task for task in storage.list_tasks()
        if task.goal_id == goal_id and task.plan_version == latest["plan_version"]
    ]
    assert len(latest_tasks) == 1
    assert latest_tasks[0].status is TaskStatus.AWAITING_APPROVAL
    pending_now = storage.list_requests(status="pending")
    assert [request.task_id for request in pending_now] == [latest_tasks[0].id]

    # The old approval no longer coordinates: the recommendation references
    # only current work.
    evaluation = manager.evaluate(goal_id)[0]
    assert evaluation.next_action == "await_approval"
    assert evaluation.evidence["approval_pending_steps"] == [
        {"task_id": latest_tasks[0].id, "step_index": 0,
         "plan_version": latest["plan_version"]},
    ]
    assert registry.get("filesystem.write").calls == 0
    assert not (sandbox / "notes.txt").exists()
    assert not (sandbox / "newer.txt").exists()
    engine.shutdown(); storage.close()


# --------------------------------------------------------------------------- #
# Test 2 — superseded mutation-lock waiter cannot permanently stall progress
# --------------------------------------------------------------------------- #


def test_superseded_lock_waiter_does_not_park_latest_plan(tmp_path: Path) -> None:
    sandbox = _sandbox(tmp_path)
    db = tmp_path / "coord-waiter.db"
    fake_time = FakeTime()
    holder, holder_lock = _hold_lock(db, sandbox)
    engine, manager, storage, registry = _engine(
        db, sandbox, max_wait=10_000.0,
        clock=fake_time.now, sleeper=InterruptSleeper(fake_time, interrupt_after=1),
    )
    goal_id = engine.submit_goal("write notes").id
    _approve(engine, goal_id)
    with pytest.raises(RuntimeError, match="simulated crash"):
        engine.run_goal(goal_id)
    waiter = manager.task_history(goal_id)[-1]
    assert waiter.plan_version == 1
    assert waiter.status is TaskStatus.RUNNING
    assert waiter.lock_wait is not None
    assert waiter.lock_wait["resource"] == "notes.txt"

    # Newer immutable plan v2 targeting a DIFFERENT resource; the external
    # lock on notes.txt is then released. Nothing current wants notes.txt.
    latest = manager.record_plan_version(
        goal_id, "direct", [_write_step("other.txt").to_dict()],
        reason="replan_new_target",
    )
    assert holder.release(holder_lock.lock_id, "proc-holder")
    holder.close()

    # Previously observed defect: repeated runs left the goal 'active' with
    # no blockers, no queued approvals, ZERO v2 tasks, forever.
    first = engine.run_goal(goal_id)
    second = engine.run_goal(goal_id)

    # The superseded waiter was fenced: terminal, wait metadata cleared,
    # durable row retained.
    waiter_after = storage.load_task(waiter.id)
    assert waiter_after is not None
    assert waiter_after.status is TaskStatus.FAILED
    assert "superseded" in (waiter_after.error or "")
    assert waiter_after.lock_wait is None

    # Latest-plan work was published and proceeds through its own approval.
    latest_tasks = [
        task for task in storage.list_tasks()
        if task.goal_id == goal_id and task.plan_version == latest["plan_version"]
    ]
    assert len(latest_tasks) == 1
    fresh = [
        request for request in storage.list_requests(status="pending")
        if request.task_id == latest_tasks[0].id
    ]
    assert len(fresh) == 1
    engine.resolve_approval_request(
        fresh[0].approval_id, ApprovalOutcome.APPROVED, actor="user:alice")

    done = engine.run_goal(goal_id)
    assert done.status.value == "completed"
    assert (sandbox / "other.txt").exists()
    assert not (sandbox / "notes.txt").exists()  # superseded waiter never mutated
    assert registry.get("filesystem.write").calls == 1
    assert second.status.value in ("blocked", "active")
    engine.shutdown(); storage.close()


# --------------------------------------------------------------------------- #
# Test 3 — noncanonical exact-version duplicate cannot influence progress
# --------------------------------------------------------------------------- #


def _completed_canonical_with_duplicate(engine, manager, storage, sandbox,
                                        duplicate_status):
    """Latest plan v1 100% complete via its canonical task, plus a legacy
    noncanonical exact-version duplicate row (pre-ADR-051 shape)."""
    goal = engine.submit_goal("write notes")
    plan = manager.record_plan_version(
        goal.id, "direct", [_write_step("notes.txt").to_dict()],
        reason="initial_plan",
    )
    canonical = engine.create_task(goal, plan_version=plan["plan_version"])
    canonical.steps = [_write_step("notes.txt")]
    storage.save_task(canonical)
    engine.run_task(canonical.id)  # -> awaiting approval
    request = engine.approval_store.list_requests(status="pending")[0]
    engine.resolve_approval_request(
        request.approval_id, ApprovalOutcome.APPROVED, actor="user:alice")
    canonical = engine.run_task(canonical.id)  # -> completed
    assert canonical.status is TaskStatus.COMPLETED

    duplicate = engine.create_task(goal, plan_version=plan["plan_version"])
    duplicate.steps = [_write_step("notes.txt")]
    duplicate.status = duplicate_status
    storage.save_task(duplicate)
    return goal, plan, canonical, storage.load_task(duplicate.id)


def test_fenced_duplicate_cannot_force_replan_or_reexecution(tmp_path: Path) -> None:
    sandbox = _sandbox(tmp_path)
    db = tmp_path / "coord-duplicate-failed.db"
    engine, manager, storage, registry = _engine(db, sandbox, max_wait=0)
    goal, plan, canonical, duplicate = _completed_canonical_with_duplicate(
        engine, manager, storage, sandbox, TaskStatus.PLANNED)

    # Someone resumes the duplicate once -> ADR-049 fence terminalizes it.
    fenced = engine.run_task(duplicate.id)
    assert fenced.status is TaskStatus.FAILED
    assert "superseded" in (fenced.error or "")

    # Previously observed defect: a fully complete latest plan evaluated as
    # 'replan' because the fenced duplicate counted as latest-plan failure.
    evaluation = manager.evaluate(goal.id)[0]
    assert evaluation.next_action == "complete"
    assert evaluation.evidence["latest_plan_failed"] == 0

    finished = engine.run_goal(goal.id)
    assert finished.status.value == "completed"
    # No manufactured plan version, no re-execution.
    assert [p["plan_version"] for p in manager.plan_history(goal.id)] == [
        plan["plan_version"]]
    assert registry.get("filesystem.write").calls == 1
    # Historical rows retained.
    assert storage.load_task(duplicate.id) is not None
    assert storage.load_task(canonical.id).status is TaskStatus.COMPLETED
    engine.shutdown(); storage.close()


def test_awaiting_duplicate_cannot_block_or_outlive_completion(tmp_path: Path) -> None:
    sandbox = _sandbox(tmp_path)
    db = tmp_path / "coord-duplicate-awaiting.db"
    engine, manager, storage, registry = _engine(db, sandbox, max_wait=0)
    goal, plan, canonical, duplicate = _completed_canonical_with_duplicate(
        engine, manager, storage, sandbox, TaskStatus.AWAITING_APPROVAL)

    # The duplicate's historical awaiting state coordinates nothing.
    evaluation = manager.evaluate(goal.id)[0]
    assert evaluation.next_action == "complete"

    finished = engine.run_goal(goal.id)
    assert finished.status.value == "completed"
    duplicate_after = storage.load_task(duplicate.id)
    assert duplicate_after.status is TaskStatus.FAILED  # fenced, retained
    assert [p["plan_version"] for p in manager.plan_history(goal.id)] == [
        plan["plan_version"]]
    assert registry.get("filesystem.write").calls == 1
    engine.shutdown(); storage.close()


# --------------------------------------------------------------------------- #
# Positive controls — current-plan coordination remains authoritative
# --------------------------------------------------------------------------- #


def test_current_approval_still_blocks_and_is_never_fenced(tmp_path: Path) -> None:
    sandbox = _sandbox(tmp_path)
    db = tmp_path / "coord-current-approval.db"
    engine, manager, storage, registry = _engine(db, sandbox, max_wait=0)
    goal_id = engine.submit_goal("write notes").id
    engine.run_goal(goal_id)
    task = manager.task_history(goal_id)[-1]
    assert task.plan_version == 1
    assert task.status is TaskStatus.AWAITING_APPROVAL

    engine.run_goal(goal_id)  # repeated cycles must not fence current work
    task_after = storage.load_task(task.id)
    assert task_after.status is TaskStatus.AWAITING_APPROVAL
    request = engine.approval_store.list_requests(status="pending")[0]
    assert request.task_id == task.id
    evaluation = manager.evaluate(goal_id)[0]
    assert evaluation.next_action == "await_approval"
    engine.shutdown(); storage.close()


def test_current_lock_waiter_still_waits_with_budget_preserved(tmp_path: Path) -> None:
    sandbox = _sandbox(tmp_path)
    db = tmp_path / "coord-current-waiter.db"
    fake_time = FakeTime()
    holder, holder_lock = _hold_lock(db, sandbox)
    engine, manager, storage, registry = _engine(
        db, sandbox, max_wait=10_000.0,
        clock=fake_time.now, sleeper=InterruptSleeper(fake_time, interrupt_after=1),
    )
    goal_id = engine.submit_goal("write notes").id
    _approve(engine, goal_id)
    with pytest.raises(RuntimeError, match="simulated crash"):
        engine.run_goal(goal_id)
    waiter = manager.task_history(goal_id)[-1]
    assert waiter.lock_wait is not None

    # Repeated normal runs while the resource stays locked: current waiter is
    # authoritative - never fenced, budget never reset.
    engine.run_goal(goal_id)
    waiter_after = storage.load_task(waiter.id)
    assert waiter_after.status is TaskStatus.RUNNING
    assert waiter_after.lock_wait is not None
    assert waiter_after.lock_wait["attempts"] >= 1
    assert waiter_after.lock_wait["deadline"] == waiter.lock_wait["deadline"]
    evaluation = manager.evaluate(goal_id)[0]
    assert evaluation.next_action == "await_lock"
    holder.close()
    engine.shutdown(); storage.close()
