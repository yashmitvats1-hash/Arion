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


# --------------------------------------------------------------------------- #
# ADR-056: plan-version attribution is correct despite post-commit race
# --------------------------------------------------------------------------- #


def test_adr056_outcome_plan_version_attribution_survives_concurrent_readopt(
        tmp_path: Path) -> None:
    """ADR-056 invariant: the strategy-outcome row for a terminal failure is
    attributed to the plan version validated by the atomic commit (N), even
    when a legal concurrent plan publication (N+1) lands between the atomic
    commit and the post-commit ``latest_plan()`` strategy-name lookup.

    Context
    -------
    ``cas_goal_terminal_fenced`` returns ``validated_plan_version`` (the
    version confirmed authoritative at commit time).  The post-commit block
    uses that value as the ``plan_version`` key for ``_record_strategy_outcome``,
    so the key is locked in before any cross-connection read.  The strategy
    *name* is fetched from a separate ``latest_plan()`` call and is therefore
    susceptible to a narrow post-commit race; however, the plan *version* in
    the outcome row is guaranteed to equal the validated N, not N+1.

    This test demonstrates and pins that guarantee deterministically.

    Injection
    ---------
    We monkeypatch ``GoalManager.latest_plan`` on the manager instance so
    that the *first* call made after the goal has transitioned to FAILED
    (i.e., the post-commit attribution call inside ``transition()``)
    triggers a ``readopt_plan``-equivalent publication of a new plan version
    N+1 with a different valid strategy, then returns the updated latest plan
    (now N+1).  This is the widened form of the sub-microsecond race that
    exists in production between the two-connection commit and the
    cognitive-store read.

    Assertions
    ----------
    - goal is durably FAILED;
    - exactly one outcome row exists for plan version N (the committed version);
    - that row carries ``outcome == "failed"``;
    - plan version N+1 has no ``outcome == "failed"`` row (N+1 was never
      evaluated or executed when the failure committed);
    - goal.version advanced by exactly one (single atomic write, no duplicate).
    """
    sandbox = _sandbox(tmp_path)
    db = tmp_path / "ffence-adr056-attr.db"
    engine, manager, storage, registry = _engine(db, sandbox, max_wait=0)
    goal, plan, task = _goal_with_failed_latest_task(engine, manager, storage)

    v1 = plan["plan_version"]
    goal_version_before = manager.get_goal(goal.id).version

    # ── Injection setup ───────────────────────────────────────────────────────
    # Wrap manager.latest_plan so the first call that occurs while the goal is
    # already FAILED (the post-commit attribution call in transition()) also
    # publishes a newer plan version using a valid but distinct strategy name.
    # This deterministically reproduces the narrow race window.
    original_latest_plan = manager.latest_plan
    injection = {"fired": False, "v2": None}

    def _latest_plan_with_injection(goal_id: str):
        result = original_latest_plan(goal_id)
        if not injection["fired"]:
            current_goal = manager.get_goal(goal_id)
            if current_goal is not None and current_goal.status.value == "failed":
                injection["fired"] = True
                # Publish N+1 via record_plan_version (the same funnel used by
                # readopt_plan and the engine planner).  "avoid_known_failures"
                # is a valid strategy name distinct from v1's "direct", so the
                # subsequent _record_strategy_outcome call will observe N+1 as
                # the latest plan and attempt to use N+1's strategy name.
                v2_record = manager.record_plan_version(
                    goal_id,
                    "avoid_known_failures",
                    [_write_step("v2-injected.txt").to_dict()],
                    reason="readopt_concurrent_with_failure",
                )
                injection["v2"] = v2_record["plan_version"]
                # Return the *updated* latest plan (N+1) to widen the race:
                # the caller inside transition() now sees N+1 and uses its
                # strategy name when building the outcome row.
                return original_latest_plan(goal_id)
        return result

    manager.latest_plan = _latest_plan_with_injection
    try:
        manager.fail_goal(
            goal.id,
            reason="max_replans_exceeded",
            expect_plan_version=v1,
        )
    finally:
        manager.latest_plan = original_latest_plan

    # ── Verify injection actually fired ───────────────────────────────────────
    assert injection["fired"], (
        "Injection did not fire: test setup is wrong or ADR-056 path was not taken"
    )
    v2 = injection["v2"]
    assert v2 is not None and v2 == v1 + 1, (
        f"Expected v2 == v1+1 == {v1 + 1}, got v2={v2}"
    )

    # ── Core invariant: plan-version attribution ───────────────────────────────
    # The outcome row MUST reference the plan version validated by the atomic
    # commit (N), not the newly published N+1.  The plan_version key is set
    # from validated_plan_version before any post-commit cross-connection read.
    goal_final = manager.get_goal(goal.id)
    assert goal_final is not None
    assert goal_final.status.value == "failed", (
        f"Goal must be durably FAILED, got {goal_final.status.value}"
    )

    all_outcomes = manager.strategy_outcomes(goal_id=goal.id, limit=50)

    # Exactly one row for plan version N with outcome "failed".
    v1_outcome_rows = [
        o for o in all_outcomes
        if o.get("plan_version") == v1
    ]
    assert len(v1_outcome_rows) == 1, (
        f"Expected exactly 1 outcome row for plan version {v1}, "
        f"got {v1_outcome_rows}"
    )
    assert v1_outcome_rows[0]["outcome"] == "failed", (
        f"Outcome for plan version {v1} must be 'failed', "
        f"got {v1_outcome_rows[0]['outcome']!r}"
    )

    # Plan version N+1 must not carry a "failed" outcome — it was never the
    # authoritative plan when the failure transition committed.
    v2_failed_rows = [
        o for o in all_outcomes
        if o.get("plan_version") == v2 and o.get("outcome") == "failed"
    ]
    assert not v2_failed_rows, (
        f"Plan version {v2} (N+1) must not be attributed as 'failed': "
        f"{v2_failed_rows}"
    )

    # goal.version advanced by exactly one (the single atomic terminal write).
    assert goal_final.version == goal_version_before + 1, (
        f"goal.version must advance by exactly 1: "
        f"before={goal_version_before}, after={goal_final.version}"
    )

    engine.shutdown()
    storage.close()
