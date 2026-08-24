"""ADR-055: evaluation-driven terminal failure is fenced to the evaluated
immutable plan version.

The failure decision carries ``evidence["latest_plan_version"]`` from
``GoalManager.evaluate()``. ADR-055 reuses the ADR-054 transition-level
lineage fence: ``fail_goal(..., expect_plan_version=...)`` must refuse a
stale failure (typed ``GoalPlanLineageError``, nothing mutated), and the
owning loop re-evaluates against current durable state. The legitimate
``max_replans_exceeded`` failure against a still-authoritative plan remains
functional.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from arion.cognition.goals import GoalPlanLineageError
from arion.orchestration.authz import ApprovalOutcome
from arion.state.models import PlanStep, TaskStatus, VerificationPolicy
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


def _goal_with_failed_latest_task(engine, manager, storage, path="notes.txt"):
    """Goal whose latest plan v1 has a FAILED implementing task: evaluate()
    recommends replan, so run_goal(max_replans=0) reaches the failure
    decision through the real production path."""
    goal = engine.submit_goal("write notes")
    plan = manager.record_plan_version(
        goal.id, "direct", [_write_step(path).to_dict()], reason="initial_plan")
    task = engine.create_task(goal, plan_version=plan["plan_version"])
    task.steps = [_write_step(path)]
    task.status = TaskStatus.FAILED
    task.error = "forced failure for fencing test"
    storage.save_task(task)
    return goal, plan, task


def _racing_failure_boundary(engine, goal, phase_name):
    """Wrap the ADR-052 ownership boundary so a concurrent immutable-plan
    commit lands in the REAL evaluate -> fail_goal window (the boundary
    injection style of the ADR-050/052/053/054 tests)."""
    original = engine._goal_run_lease_current
    fired = {"done": False}

    def boundary(goal_id, claim, phase):
        if phase == phase_name and not fired["done"]:
            fired["done"] = True
            manager = engine.goal_manager
            manager.record_plan_version(
                goal.id, "direct", [_write_step("fresh.txt").to_dict()],
                reason="operator_replan")
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


def test_stale_plan_version_cannot_fail_goal(tmp_path: Path) -> None:
    sandbox = _sandbox(tmp_path)
    db = tmp_path / "ffence-single.db"
    engine, manager, storage, registry = _engine(db, sandbox, max_wait=0)
    goal, plan, task = _goal_with_failed_latest_task(engine, manager, storage)

    restore = _racing_failure_boundary(engine, goal, "goal failure")
    g = engine.run_goal(goal.id, max_replans=0)
    restore()

    # The stale v1 failure decision must NOT terminally fail the goal.
    assert g.status.value != "failed", (
        f"goal failed on stale plan evidence (status={g.status.value})")
    # No false failure provenance for the newer plan.
    assert "failed" not in {
        outcome for (version, outcome) in _outcome_rows(manager, goal.id)
        if version == 2}
    # The stale decision mutated nothing historical...
    task_after = storage.load_task(task.id)
    assert task_after.status is TaskStatus.FAILED
    assert task_after.error == "forced failure for fencing test"
    # ...and current durable state was re-evaluated against the new lineage.
    assert g.progress_metadata.get("evidence", {}).get(
        "latest_plan_version") == 2

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
    assert done.status.value == "completed"
    assert (2, "succeeded") in _outcome_rows(manager, goal.id)  # true outcome
    engine.shutdown(); storage.close()


# --------------------------------------------------------------------------- #
# Bulk/shared path is fenced identically
# --------------------------------------------------------------------------- #


def test_bulk_stale_failure_is_fenced(tmp_path: Path) -> None:
    sandbox = _sandbox(tmp_path)
    db = tmp_path / "ffence-bulk.db"
    engine, manager, storage, registry = _engine(db, sandbox, max_wait=0)
    goal, plan, task = _goal_with_failed_latest_task(engine, manager, storage)

    restore = _racing_failure_boundary(engine, goal, "shared goal failure")
    results = engine.run_goals([goal.id], max_replans=0)
    restore()

    g = results[goal.id]
    assert g.status.value != "failed"
    assert "failed" not in {
        outcome for (version, outcome) in _outcome_rows(manager, goal.id)
        if version == 2}

    # The shared loop stopped cleanly; the next invocation proceeds with v2.
    results2 = engine.run_goals([goal.id], max_replans=5)
    g2 = results2[goal.id]
    assert g2.status.value != "failed"
    v2_tasks = [t for t in storage.list_tasks()
                if t.goal_id == goal.id and t.plan_version == 2]
    assert len(v2_tasks) == 1
    assert v2_tasks[0].status is TaskStatus.AWAITING_APPROVAL
    engine.shutdown(); storage.close()


# --------------------------------------------------------------------------- #
# Legitimate failure still works (positive control, real path)
# --------------------------------------------------------------------------- #


def test_legitimate_max_replans_failure_still_fails(tmp_path: Path) -> None:
    sandbox = _sandbox(tmp_path)
    db = tmp_path / "ffence-legit.db"
    engine, manager, storage, registry = _engine(db, sandbox, max_wait=0)
    goal, plan, task = _goal_with_failed_latest_task(engine, manager, storage)

    g = engine.run_goal(goal.id, max_replans=0)  # v1 stays latest: no race

    assert g.status.value == "failed"
    assert g.last_replan_reason == "max_replans_exceeded"
    assert (1, "failed") in _outcome_rows(manager, goal.id)
    engine.shutdown(); storage.close()


# --------------------------------------------------------------------------- #
# Mismatch fails closed with zero side effects
# --------------------------------------------------------------------------- #


def test_failure_mismatch_fails_closed_without_side_effects(
        tmp_path: Path) -> None:
    sandbox = _sandbox(tmp_path)
    db = tmp_path / "ffence-mismatch.db"
    engine, manager, storage, registry = _engine(db, sandbox, max_wait=0)
    goal, plan, task = _goal_with_failed_latest_task(engine, manager, storage)
    before = manager.get_goal(goal.id)
    outcomes_before = _outcome_rows(manager, goal.id)
    task_before = storage.load_task(task.id)

    with pytest.raises(GoalPlanLineageError):
        manager.fail_goal(goal.id, reason="max_replans_exceeded",
                          expect_plan_version=plan["plan_version"] + 5)

    after = manager.get_goal(goal.id)
    assert after.status.value == before.status.value   # not failed...
    assert after.status.value != "completed"           # ...and not completed
    assert after.version == before.version             # row untouched
    assert after.blockers == before.blockers           # no manufactured blocker
    assert after.last_replan_reason == before.last_replan_reason
    assert _outcome_rows(manager, goal.id) == outcomes_before    # no outcomes
    assert storage.load_task(task.id).to_dict() == task_before.to_dict()
    # The caller re-evaluates from current durable state and may then proceed:
    evaluation = manager.evaluate(goal.id)[0]
    assert evaluation.next_action == "replan"          # fresh decision on v1
    engine.shutdown(); storage.close()
