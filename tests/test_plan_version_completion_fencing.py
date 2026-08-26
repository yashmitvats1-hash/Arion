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
    #
    # ADR-056: the atomic path delegates to cas_goal_terminal_fenced (which
    # reads goal_plans + updates goals in one BEGIN IMMEDIATE).  We patch
    # that method to inject the forced miss and concurrent v2 publication,
    # preserving the same invariant: a CAS retry never reuses stale plan
    # authority from an earlier attempt.
    other_storage = SQLiteStorage(db)
    other_gm = GoalManager(storage=other_storage,
                           cognitive_store=manager.cognitive_store,
                           events=None,
                           strategy_selector=manager.strategy_selector,
                           progress_evaluator=type(manager.progress_evaluator)(),
                           world_monitor=None)
    real_fenced_cas = storage.cas_goal_terminal_fenced
    attempts = {"n": 0}

    def fenced_cas_with_forced_retry(goal_id, expected_goal_version,
                                     expect_plan_version, fields):
        if fields.get("status") == "completed" and attempts["n"] == 0:
            attempts["n"] += 1
            other_gm.pause(goal_id)    # bumps goal.version
            other_gm.resume(goal_id)   # back to ACTIVE, version bumped again
            other_gm.record_plan_version(
                goal_id, "direct", [_write_step("newer.txt").to_dict()],
                reason="replan_retry_race")
            return ("cas_miss", None)  # force the CAS miss -> transition retries
        return real_fenced_cas(goal_id, expected_goal_version,
                               expect_plan_version, fields)

    storage.cas_goal_terminal_fenced = fenced_cas_with_forced_retry
    try:
        with pytest.raises(GoalPlanLineageError):
            manager.complete_goal(goal.id, reason="all_work_complete",
                                  expect_plan_version=1)
    finally:
        storage.cas_goal_terminal_fenced = real_fenced_cas

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


# --------------------------------------------------------------------------- #
# ADR-056: plan-version attribution is correct despite post-commit race
# --------------------------------------------------------------------------- #


def test_adr056_outcome_plan_version_attribution_survives_concurrent_readopt(
        tmp_path: Path) -> None:
    """ADR-056 invariant: the strategy-outcome row for a terminal completion is
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

    This test demonstrates and pins that guarantee deterministically for the
    completion path, mirroring the equivalent failure-path test in
    ``test_plan_version_failure_fencing.py``.

    Injection
    ---------
    We monkeypatch ``GoalManager.latest_plan`` on the manager instance so
    that the *first* call made after the goal has transitioned to COMPLETED
    (i.e., the post-commit attribution call inside ``transition()``)
    triggers a plan publication of a new plan version N+1 with a different
    valid strategy, then returns the updated latest plan (now N+1).  This is
    the widened form of the sub-microsecond race that exists in production
    between the two-connection commit and the cognitive-store read.

    The race is legal for completed goals only in this deterministic form:
    ``record_plan_version`` (not ``readopt_plan``, which blocks on COMPLETED)
    is called directly to publish N+1 on the same ``GoalManager`` instance,
    which has access to both the storage and cognitive connections.

    Assertions
    ----------
    - the injection actually fired;
    - plan version N+1 was successfully published (v2 == v1 + 1);
    - the goal is durably COMPLETED;
    - exactly one outcome row exists for plan version N (the committed version);
    - that row carries ``outcome == "succeeded"``;
    - plan version N+1 has no ``outcome == "succeeded"`` row — N+1 was never
      the plan whose work caused the terminal completion;
    - goal.version advanced by exactly one (single atomic write, no duplicate).
    """
    sandbox = _sandbox(tmp_path)
    db = tmp_path / "fence-adr056-attr.db"
    engine, manager, storage, registry = _engine(db, sandbox, max_wait=0)

    # Drive the goal to the point where v1 work is complete and the engine
    # is about to call complete_goal — using the existing file helpers.
    goal = _drive_to_approved_ready_task(engine, manager)
    _execute_v1_without_completing_goal(engine, manager, goal)

    v1 = manager.latest_plan(goal.id)["plan_version"]
    goal_version_before = manager.get_goal(goal.id).version

    # ── Injection setup ───────────────────────────────────────────────────────
    # Wrap manager.latest_plan so the first call made while the goal is already
    # COMPLETED (the post-commit attribution call inside transition()) also
    # publishes a newer plan version using a valid but distinct strategy name,
    # then returns the updated latest plan (now N+1).  This deterministically
    # reproduces the narrow post-commit race window.
    original_latest_plan = manager.latest_plan
    injection = {"fired": False, "v2": None}

    def _latest_plan_with_injection(goal_id: str):
        result = original_latest_plan(goal_id)
        if not injection["fired"]:
            current_goal = manager.get_goal(goal_id)
            if current_goal is not None and current_goal.status.value == "completed":
                injection["fired"] = True
                # Publish N+1 via record_plan_version — the same funnel used
                # by the engine planner.  "avoid_known_failures" is a valid
                # strategy name distinct from v1's "direct", so the subsequent
                # _record_strategy_outcome call will observe N+1 as the latest
                # plan and use N+1's strategy name when building the outcome row.
                v2_record = manager.record_plan_version(
                    goal_id,
                    "avoid_known_failures",
                    [_write_step("v2-injected.txt").to_dict()],
                    reason="readopt_concurrent_with_completion",
                )
                injection["v2"] = v2_record["plan_version"]
                # Return the *updated* latest plan (N+1) to widen the race:
                # transition() now sees N+1 and uses its strategy name when
                # building the outcome row for the plan_version key.
                return original_latest_plan(goal_id)
        return result

    manager.latest_plan = _latest_plan_with_injection
    try:
        manager.complete_goal(
            goal.id,
            reason="all_work_complete",
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
    assert goal_final.status.value == "completed", (
        f"Goal must be durably COMPLETED, got {goal_final.status.value}"
    )

    all_outcomes = manager.strategy_outcomes(goal_id=goal.id, limit=50)

    # Exactly one outcome row for plan version N with outcome "succeeded".
    v1_outcome_rows = [
        o for o in all_outcomes
        if o.get("plan_version") == v1
    ]
    assert len(v1_outcome_rows) == 1, (
        f"Expected exactly 1 outcome row for plan version {v1}, "
        f"got {v1_outcome_rows}"
    )
    assert v1_outcome_rows[0]["outcome"] == "succeeded", (
        f"Outcome for plan version {v1} must be 'succeeded', "
        f"got {v1_outcome_rows[0]['outcome']!r}"
    )

    # Plan version N+1 must not carry a "succeeded" outcome — it was never the
    # authoritative plan when the completion transition committed.
    v2_succeeded_rows = [
        o for o in all_outcomes
        if o.get("plan_version") == v2 and o.get("outcome") == "succeeded"
    ]
    assert not v2_succeeded_rows, (
        f"Plan version {v2} (N+1) must not be attributed as 'succeeded': "
        f"{v2_succeeded_rows}"
    )

    # goal.version advanced by exactly one (the single atomic terminal write).
    assert goal_final.version == goal_version_before + 1, (
        f"goal.version must advance by exactly 1: "
        f"before={goal_version_before}, after={goal_final.version}"
    )

    engine.shutdown()
    storage.close()
