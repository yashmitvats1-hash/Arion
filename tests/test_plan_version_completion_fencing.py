"""ADR-054: goal completion is fenced to the evaluated immutable plan version.

The completion decision carries ``evidence["latest_plan_version"]`` from
``GoalManager.evaluate()``. ADR-054 requires that version to still be the
authoritative latest plan at the moment ``transition()`` commits the
irreversible terminal COMPLETED state - checked INSIDE the CAS retry loop so
retries never reuse stale plan authority. On mismatch the transition fails
closed without mutating anything, and the owning loop re-evaluates.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from arion.cognition.goals import GoalManager, GoalPlanLineageError
from arion.orchestration.authz import ApprovalOutcome
from arion.state.models import PlanStep, TaskStatus, VerificationPolicy
from arion.state.store import SQLiteStorage
from tests.test_lock_waiting import _engine, _sandbox


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


def _drive_to_approved_ready_task(engine, manager):
    """Goal with an approved v1 write step, ready to execute."""
    goal = engine.submit_goal("write notes")
    engine.run_goal(goal.id)  # v1 task -> awaiting approval
    request = engine.approval_store.list_requests(status="pending")[0]
    engine.resolve_approval_request(
        request.approval_id, ApprovalOutcome.APPROVED, actor="user:alice")
    return goal


def _execute_v1_without_completing_goal(engine, manager, goal):
    """Execute the approved v1 task directly so every v1 step is handled but
    the goal stays ACTIVE (no goal-loop completion decision has run)."""
    task = manager.pending_task(goal.id)
    assert task is not None
    task = engine.run_task(task.id)
    assert task.status is TaskStatus.COMPLETED
    return task


def _racing_boundary(engine, goal, phase_name, step_dict):
    """Wrap the ADR-052 ownership boundary so a concurrent immutable-plan
    commit lands in the REAL evaluate -> complete_goal window (the boundary
    injection style of the ADR-050/052/053 tests). The commit uses the
    legitimate public lineage funnel."""
    original = engine._goal_run_lease_current
    fired = {"done": False}

    def boundary(goal_id, claim, phase):
        if phase == phase_name and not fired["done"]:
            fired["done"] = True
            manager = engine.goal_manager
            manager.record_plan_version(goal.id, "direct", [step_dict],
                                        reason="replan_rollback_v1")
        return original(goal_id, claim, phase)

    engine._goal_run_lease_current = boundary
    return lambda: setattr(engine, "_goal_run_lease_current", original)


def _outcome_rows(manager, goal_id):
    return {
        (row.get("plan_version"), row.get("outcome"))
        for row in manager.strategy_outcomes(goal_id=goal_id, limit=50)
    }


# --------------------------------------------------------------------------- #
# Primary invariant — single-goal path
# --------------------------------------------------------------------------- #


def test_stale_plan_version_cannot_complete_goal(tmp_path: Path) -> None:
    sandbox = _sandbox(tmp_path)
    db = tmp_path / "fence-single.db"
    engine, manager, storage, registry = _engine(db, sandbox, max_wait=0)
    goal = _drive_to_approved_ready_task(engine, manager)

    restore = _racing_boundary(
        engine, goal, "goal completion", _write_step("newer.txt").to_dict())
    g = engine.run_goal(goal.id)
    restore()

    # The stale v1 completion decision must NOT terminally complete the goal.
    assert g.status.value != "completed", (
        f"goal completed on stale plan evidence (status={g.status.value})")
    # No false success provenance for the newer plan.
    assert "succeeded" not in {
        outcome for (version, outcome) in _outcome_rows(manager, goal.id)
        if version == 2}

    # Latest-plan work proceeds normally afterwards.
    latest = manager.latest_plan(goal.id)
    assert latest["plan_version"] == 2
    v2_tasks = [t for t in storage.list_tasks()
                if t.goal_id == goal.id and t.plan_version == 2]
    assert len(v2_tasks) == 1
    assert v2_tasks[0].status is TaskStatus.AWAITING_APPROVAL  # own approval
    fresh = [r for r in storage.list_requests(status="pending")
             if r.task_id == v2_tasks[0].id]
    assert len(fresh) == 1
    engine.resolve_approval_request(
        fresh[0].approval_id, ApprovalOutcome.APPROVED, actor="user:bob")
    done = engine.run_goal(goal.id)
    assert done.status.value == "completed"           # NOW completion is legal
    assert (sandbox / "notes.txt").exists()
    assert (sandbox / "newer.txt").exists()
    assert (2, "succeeded") in _outcome_rows(manager, goal.id)  # true outcome
    engine.shutdown(); storage.close()


# --------------------------------------------------------------------------- #
# Bulk/shared path is fenced identically
# --------------------------------------------------------------------------- #


def test_bulk_stale_completion_is_fenced(tmp_path: Path) -> None:
    sandbox = _sandbox(tmp_path)
    db = tmp_path / "fence-bulk.db"
    engine, manager, storage, registry = _engine(db, sandbox, max_wait=0)
    goal = _drive_to_approved_ready_task(engine, manager)

    restore = _racing_boundary(
        engine, goal, "shared goal completion",
        _write_step("newer.txt").to_dict())
    results = engine.run_goals([goal.id])
    restore()

    g = results[goal.id]
    assert g.status.value != "completed"
    assert "succeeded" not in {
        outcome for (version, outcome) in _outcome_rows(manager, goal.id)
        if version == 2}

    # The shared loop stopped cleanly; the next invocation proceeds with v2.
    results2 = engine.run_goals([goal.id])
    g2 = results2[goal.id]
    assert g2.status.value != "completed"
    v2_tasks = [t for t in storage.list_tasks()
                if t.goal_id == goal.id and t.plan_version == 2]
    assert len(v2_tasks) == 1
    assert v2_tasks[0].status is TaskStatus.AWAITING_APPROVAL
    engine.shutdown(); storage.close()


# --------------------------------------------------------------------------- #
# Normal completion still works (positive control, evaluated version passed)
# --------------------------------------------------------------------------- #


def test_normal_completion_with_expected_plan_version_still_completes(
        tmp_path: Path) -> None:
    sandbox = _sandbox(tmp_path)
    db = tmp_path / "fence-normal.db"
    engine, manager, storage, registry = _engine(db, sandbox, max_wait=0)
    goal = _drive_to_approved_ready_task(engine, manager)

    g = engine.run_goal(goal.id)  # no concurrent commit: latest stays v1

    assert g.status.value == "completed"
    assert (1, "succeeded") in _outcome_rows(manager, goal.id)
    engine.shutdown(); storage.close()


# --------------------------------------------------------------------------- #
# CAS retry must re-check plan lineage (no stale authority reuse on retry)
# --------------------------------------------------------------------------- #


def test_transition_retry_rechecks_plan_lineage(tmp_path: Path) -> None:
    sandbox = _sandbox(tmp_path)
    db = tmp_path / "fence-retry.db"
    engine, manager, storage, registry = _engine(db, sandbox, max_wait=0)
    goal = _drive_to_approved_ready_task(engine, manager)
    _execute_v1_without_completing_goal(engine, manager, goal)

    # A second connection bumps goal.version (pause -> resume) AND advances
    # plan lineage to v2 exactly while the first CAS attempt is in flight;
    # the forced CAS miss makes transition() retry with FRESH state.
    other_storage = SQLiteStorage(db)
    other_gm = GoalManager(storage=other_storage,
                           cognitive_store=manager.cognitive_store,
                           events=None,
                           strategy_selector=manager.strategy_selector,
                           progress_evaluator=type(manager.progress_evaluator)(),
                           world_monitor=None)
    real_cas = storage.cas_goal_fields
    attempts = {"n": 0}

    def cas_with_forced_retry(goal_id, expected_version, fields):
        if "status" in fields and fields.get("status") == "completed" \
                and attempts["n"] == 0:
            attempts["n"] += 1
            other_gm.pause(goal_id)    # bumps goal.version
            other_gm.resume(goal_id)   # back to ACTIVE, version bumped again
            other_gm.record_plan_version(
                goal_id, "direct", [_write_step("newer.txt").to_dict()],
                reason="replan_retry_race")
            return False               # force the CAS miss -> transition retries
        return real_cas(goal_id, expected_version, fields)

    storage.cas_goal_fields = cas_with_forced_retry
    try:
        with pytest.raises(GoalPlanLineageError):
            manager.complete_goal(goal.id, reason="all_work_complete",
                                  expect_plan_version=1)
    finally:
        storage.cas_goal_fields = real_cas

    assert attempts["n"] == 1  # the retry actually happened
    g = manager.get_goal(goal.id)
    assert g.status.value == "active"    # pause+resume only; no completion
    assert "succeeded" not in {
        outcome for (version, outcome) in _outcome_rows(manager, goal.id)
        if version == 2}
    other_storage.close()
    engine.shutdown(); storage.close()


# --------------------------------------------------------------------------- #
# Mismatch fails closed with no side effects
# --------------------------------------------------------------------------- #


def test_mismatch_fails_closed_without_side_effects(tmp_path: Path) -> None:
    sandbox = _sandbox(tmp_path)
    db = tmp_path / "fence-mismatch.db"
    engine, manager, storage, registry = _engine(db, sandbox, max_wait=0)

    goal = engine.submit_goal("write more")
    plan = manager.record_plan_version(
        goal.id, "direct", [_write_step("more.txt").to_dict()],
        reason="initial_plan")
    task = engine.create_task(goal, plan_version=plan["plan_version"])
    task.steps = [_write_step("more.txt")]
    task.status = TaskStatus.PLANNED
    storage.save_task(task)
    before = manager.get_goal(goal.id)
    outcomes_before = _outcome_rows(manager, goal.id)
    task_before = storage.load_task(task.id)

    with pytest.raises(GoalPlanLineageError):
        manager.complete_goal(goal.id, reason="all_work_complete",
                              expect_plan_version=plan["plan_version"] + 5)

    after = manager.get_goal(goal.id)
    assert after.status.value == before.status.value   # not completed...
    assert after.status.value != "failed"              # ...and not failed
    assert after.version == before.version             # row untouched
    assert after.blockers == before.blockers           # no manufactured blocker
    assert _outcome_rows(manager, goal.id) == outcomes_before    # no outcomes
    assert storage.load_task(task.id).to_dict() == task_before.to_dict()
    # The caller re-evaluates from current durable state and may then proceed:
    evaluation = manager.evaluate(goal.id)[0]
    assert evaluation.next_action == "continue"        # outstanding v1 work
    engine.shutdown(); storage.close()
