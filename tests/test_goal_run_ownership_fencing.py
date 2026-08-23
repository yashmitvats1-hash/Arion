"""ADR-052: stale goal-run owners cannot continue authority-bearing work."""

from __future__ import annotations

import threading
import time
from pathlib import Path

from arion.state.approvals import ApprovalStatus
from arion.state.models import (
    PlanStep,
    StepStatus,
    TaskStatus,
    VerificationPolicy,
)
from arion.state.recovery import MutationRecovery, RecoveryStatus
from tests.test_goal_run_lease import (
    _AppendOnce,
    _BlockingPlanner,
    _close,
    _engine,
)
from tests.test_lock_waiting import (
    _engine as _approval_engine,
    _sandbox,
)


def _step(index: int, path: str, *, depends_on: list[int] | None = None) -> PlanStep:
    return PlanStep(
        index=index,
        intent=f"append {path}",
        capability="goal.append",
        action="append",
        scope="goal:write",
        params={"path": path},
        verification=VerificationPolicy("non_empty"),
        max_attempts=1,
        depends_on=list(depends_on or []),
    )


class _BlockAfterFirstEffect(_AppendOnce):
    """Apply the first effect, then hold capability return deterministically."""

    def __init__(self, effects: Path) -> None:
        super().__init__(effects)
        self.first_effect = threading.Event()
        self.release_first = threading.Event()

    def execute(self, action, params):
        observation = super().execute(action, params)
        if self.calls == 1:
            self.first_effect.set()
            self.release_first.wait(timeout=10)
        return observation


def test_stale_goal_run_owner_cannot_publish_replan_after_takeover(
    tmp_path: Path,
) -> None:
    """A resumed stale planner cannot turn one logical effect into plan v2."""
    db = tmp_path / "stale-plan-owner.db"
    effects = tmp_path / "stale-plan-effects.log"
    capability = _BlockAfterFirstEffect(effects)
    stale_planner = _BlockingPlanner(block=True)
    current_planner = _BlockingPlanner()
    stale, stale_manager, stale_store, stale_cognition = _engine(
        db, stale_planner, capability, lease=0.12
    )
    current, current_manager, current_store, current_cognition = _engine(
        db, current_planner, capability, lease=0.12
    )
    goal = stale.submit_goal("append exactly once")

    acquired = threading.Event()
    captured_claim: dict[str, object] = {}
    original_acquire = stale._acquire_goal_run_lease

    def capture_acquire(goal_id: str):
        claim = original_acquire(goal_id)
        captured_claim["value"] = claim
        acquired.set()
        return claim

    stale._acquire_goal_run_lease = capture_acquire
    results: dict[str, object] = {}
    errors: list[tuple[str, BaseException]] = []

    def run_stale() -> None:
        try:
            results["stale"] = stale.run_goal(goal.id)
        except BaseException as exc:
            errors.append(("stale", exc))

    def run_current() -> None:
        try:
            results["current"] = current.run_goal(goal.id)
        except BaseException as exc:
            errors.append(("current", exc))

    stale_thread = threading.Thread(target=run_stale, name="stale-goal-owner")
    stale_thread.start()
    assert acquired.wait(timeout=3)
    assert stale_planner.started.wait(timeout=3)
    stale_claim = captured_claim["value"]
    assert stale_claim is not None

    # Model full process suspension: its heartbeat stops, the exact lease
    # expires, and another engine legitimately reclaims the goal.
    stale._stop_lock_heartbeat(stale_claim[1])
    time.sleep(0.18)

    current_thread = threading.Thread(target=run_current, name="current-goal-owner")
    current_thread.start()
    assert capability.first_effect.wait(timeout=5)

    # The current owner has applied the first external effect but has not yet
    # returned from capability code. Resume the stale planner stack now.
    stale_planner.release.set()
    stale_thread.join(timeout=5)
    assert not stale_thread.is_alive()

    capability.release_first.set()
    current_thread.join(timeout=10)
    assert not current_thread.is_alive()

    # Drive any legitimate post-capability convergence. Before ADR-052 this
    # reconstructs stale-published plan v2 and repeats the append.
    final = current.run_goal(goal.id)

    plans = stale_manager.plan_history(goal.id)
    tasks = [
        task for task in stale_store.list_tasks()
        if task.goal_id == goal.id
    ]
    exact_latest = [
        task for task in tasks
        if task.plan_version == plans[-1]["plan_version"]
    ]
    publication_events = [
        event for event in stale_store.list_events()
        if event.kind in ("plan.versioned", "plan.produced")
    ]

    assert errors == []
    assert [plan["plan_version"] for plan in plans] == [1]
    assert capability.calls == 1
    assert effects.read_text(encoding="utf-8").splitlines() == ["effect-1"]
    assert final.status.value == "completed"
    assert len(tasks) == 1
    assert len(exact_latest) == 1
    assert exact_latest[0].status is TaskStatus.COMPLETED
    assert [event.kind for event in publication_events].count("plan.versioned") == 1
    assert [event.kind for event in publication_events].count("plan.produced") == 1

    _close(stale, stale_store, stale_cognition)
    _close(current, current_store, current_cognition)


def test_stale_owner_stops_before_next_capability_dispatch(
    tmp_path: Path,
) -> None:
    """A result already in flight is retained; the stale owner starts no next step."""
    db = tmp_path / "stale-dispatch.db"
    effects = tmp_path / "stale-dispatch-effects.log"
    capability = _BlockAfterFirstEffect(effects)
    stale, manager, stale_store, stale_cognition = _engine(
        db, _BlockingPlanner(forbidden=True), capability, lease=0.12
    )
    current, _current_manager, current_store, current_cognition = _engine(
        db, _BlockingPlanner(forbidden=True), capability, lease=0.12
    )
    goal = stale.submit_goal("append two ordered records")
    steps = [_step(0, "first.txt"), _step(1, "second.txt", depends_on=[0])]
    plan = manager.record_plan_version(
        goal.id, "direct", [step.to_dict() for step in steps],
        reason="initial_plan",
    )
    task = stale.create_task(goal, plan_version=plan["plan_version"])
    task.steps = steps
    task.status = TaskStatus.PLANNED
    stale_store.save_task(task)

    acquired = threading.Event()
    captured: dict[str, object] = {}
    original_acquire = stale._acquire_goal_run_lease

    def capture_acquire(goal_id: str):
        claim = original_acquire(goal_id)
        captured["claim"] = claim
        acquired.set()
        return claim

    stale._acquire_goal_run_lease = capture_acquire
    errors: list[BaseException] = []

    def run_stale() -> None:
        try:
            stale.run_task(task.id)
        except BaseException as exc:
            errors.append(exc)

    worker = threading.Thread(target=run_stale, name="stale-task-owner")
    worker.start()
    assert acquired.wait(timeout=3)
    assert capability.first_effect.wait(timeout=5)
    stale_claim = captured["claim"]
    stale._stop_lock_heartbeat(stale_claim[1])
    time.sleep(0.18)

    current_claim = current._acquire_goal_run_lease(goal.id)
    assert current_claim is not None
    capability.release_first.set()
    worker.join(timeout=10)
    assert not worker.is_alive()
    assert errors == []

    # The first invocation was already in flight and is durably retained. The
    # stale owner must stop before dispatching the second step.
    after_stale = stale_store.load_task(task.id)
    assert capability.calls == 1
    assert effects.read_text(encoding="utf-8").splitlines() == ["effect-1"]
    assert after_stale.status is TaskStatus.RUNNING
    assert [step.status for step in after_stale.steps] == [
        StepStatus.SUCCEEDED, StepStatus.PENDING,
    ]

    completed = current._run_task_owned(
        task.id, goal_run_claim=current_claim
    )
    assert completed.status is TaskStatus.COMPLETED
    assert capability.calls == 2
    assert effects.read_text(encoding="utf-8").splitlines() == [
        "effect-1", "effect-2",
    ]

    current._release_goal_run_lease(goal.id, current_claim)
    _close(stale, stale_store, stale_cognition)
    _close(current, current_store, current_cognition)


def test_stale_owner_cannot_publish_stored_plan_task_after_takeover(
    tmp_path: Path,
) -> None:
    """A stale no-task observation cannot pass the exact task claim boundary."""
    db = tmp_path / "stale-reconstruction.db"
    capability = _AppendOnce(tmp_path / "unused-reconstruction.log")
    stale, stale_manager, stale_store, stale_cognition = _engine(
        db, _BlockingPlanner(forbidden=True), capability, lease=0.12
    )
    current, current_manager, current_store, current_cognition = _engine(
        db, _BlockingPlanner(forbidden=True), capability, lease=0.12
    )
    goal = stale.submit_goal("reconstruct exactly once")
    step = _step(0, "stored.txt")
    stale_manager.record_plan_version(
        goal.id, "direct", [step.to_dict()], reason="initial_plan"
    )

    stale_claim = stale._acquire_goal_run_lease(goal.id)
    checked = threading.Event()
    proceed = threading.Event()
    original_history = stale_manager.task_history

    def stale_history(goal_id: str):
        observed = original_history(goal_id)
        checked.set()
        proceed.wait(timeout=5)
        return observed

    stale_manager.task_history = stale_history
    stale_result: dict[str, object] = {}
    errors: list[BaseException] = []

    def reconstruct_stale() -> None:
        try:
            stale_result["task"] = stale._plan_for_goal(
                goal.id, goal_run_claim=stale_claim
            )
        except BaseException as exc:
            errors.append(exc)

    worker = threading.Thread(target=reconstruct_stale)
    worker.start()
    assert checked.wait(timeout=3)
    stale._stop_lock_heartbeat(stale_claim[1])
    time.sleep(0.18)

    current_claim = current._acquire_goal_run_lease(goal.id)
    assert current_claim is not None
    published = current._plan_for_goal(
        goal.id, goal_run_claim=current_claim
    )
    proceed.set()
    worker.join(timeout=5)
    stale_manager.task_history = original_history

    assert not worker.is_alive()
    assert errors == []
    assert stale_result["task"] is None
    assert published is not None
    exact = [
        task for task in stale_store.list_tasks()
        if task.goal_id == goal.id and task.plan_version == 1
    ]
    publication_kinds = [
        event.kind for event in stale_store.list_events()
        if event.kind in ("task.created", "plan.produced")
    ]
    assert len(exact) == 1
    assert exact[0].id == published.id
    assert publication_kinds.count("task.created") == 1
    assert publication_kinds.count("plan.produced") == 1
    assert capability.calls == 0

    current._release_goal_run_lease(goal.id, current_claim)
    stale._release_goal_run_lease(
        goal.id, (stale_claim[0], None)
    )
    _close(stale, stale_store, stale_cognition)
    _close(current, current_store, current_cognition)


def test_bulk_goal_ownership_loss_is_isolated_per_goal(
    tmp_path: Path,
) -> None:
    """One lost bulk claim does not stop an independently owned goal."""
    db = tmp_path / "bulk-isolation.db"
    effects = tmp_path / "bulk-isolation-effects.log"
    capability = _AppendOnce(effects)
    owner, manager, owner_store, owner_cognition = _engine(
        db, _BlockingPlanner(), capability, lease=0.12
    )
    peer, _peer_manager, peer_store, peer_cognition = _engine(
        db, _BlockingPlanner(forbidden=True), capability, lease=0.12
    )
    lost_goal = owner.submit_goal("lost bulk goal")
    live_goal = owner.submit_goal("live bulk goal")
    lost_claim = owner._acquire_goal_run_lease(lost_goal.id)
    live_claim = owner._acquire_goal_run_lease(live_goal.id)
    assert lost_claim is not None and live_claim is not None

    owner._stop_lock_heartbeat(lost_claim[1])
    time.sleep(0.18)
    peer_claim = peer._acquire_goal_run_lease(lost_goal.id)
    assert peer_claim is not None

    results = owner._run_goals_owned(
        [lost_goal.id, live_goal.id],
        goal_run_claims={
            lost_goal.id: lost_claim,
            live_goal.id: live_claim,
        },
    )

    assert results[lost_goal.id].status.value == "active"
    assert results[live_goal.id].status.value == "completed"
    assert manager.plan_history(lost_goal.id) == []
    assert [plan["plan_version"] for plan in manager.plan_history(
        live_goal.id
    )] == [1]
    assert [task for task in owner_store.list_tasks()
            if task.goal_id == lost_goal.id] == []
    assert len([task for task in owner_store.list_tasks()
                if task.goal_id == live_goal.id]) == 1
    assert capability.calls == 1
    assert effects.read_text(encoding="utf-8").splitlines() == ["effect-1"]

    peer._release_goal_run_lease(lost_goal.id, peer_claim)
    owner._release_goal_run_lease(live_goal.id, live_claim)
    owner._release_goal_run_lease(lost_goal.id, (lost_claim[0], None))
    _close(owner, owner_store, owner_cognition)
    _close(peer, peer_store, peer_cognition)


def test_goal_run_guard_requires_the_exact_goal_lease_identity(
    tmp_path: Path,
) -> None:
    """A live lease for another goal cannot mutate this goal's task or blockers."""
    db = tmp_path / "exact-goal-identity.db"
    capability = _AppendOnce(tmp_path / "exact-identity-effects.log")
    engine, manager, storage, cognition = _engine(
        db, _BlockingPlanner(forbidden=True), capability
    )
    first = engine.submit_goal("first exact goal")
    second = engine.submit_goal("second exact goal")
    step = _step(0, "second.txt")
    plan = manager.record_plan_version(
        second.id, "direct", [step.to_dict()], reason="initial_plan"
    )
    task = engine.create_task(second, plan_version=plan["plan_version"])
    task.steps = [step]
    task.status = TaskStatus.PLANNED
    storage.save_task(task)
    manager.set_blocked(
        second.id,
        {"type": "operator_hold", "reason": "preserve me"},
        reason="operator_hold",
    )
    before_task = storage.load_task(task.id)
    before_goal = manager.get_goal(second.id)

    wrong_claim = engine._acquire_goal_run_lease(first.id)
    assert wrong_claim is not None
    returned = engine._run_task_owned(
        task.id, goal_run_claim=wrong_claim
    )
    after_task = storage.load_task(task.id)
    after_goal = manager.get_goal(second.id)

    assert returned.id == task.id
    assert after_task.status is before_task.status is TaskStatus.PLANNED
    assert after_task.revision == before_task.revision
    assert after_task.steps[0].status is StepStatus.PENDING
    assert after_goal.status == before_goal.status
    assert after_goal.version == before_goal.version
    assert after_goal.blockers == before_goal.blockers
    assert capability.calls == 0
    assert storage.list_work(task_id=task.id) == []

    engine._release_goal_run_lease(first.id, wrong_claim)
    _close(engine, storage, cognition)


def test_stale_owner_does_not_change_current_approval_or_recovery_state(
    tmp_path: Path,
) -> None:
    """Loss is checked before superseded-task, approval, or recovery mutation."""
    sandbox = _sandbox(tmp_path)
    db = tmp_path / "cross-owner-authority.db"
    stale, manager, storage, _registry = _approval_engine(
        db, sandbox, max_wait=0
    )
    goal_id = stale.submit_goal("write notes").id
    stale.run_goal(goal_id)
    old_task = manager.task_history(goal_id)[-1]
    old_request = storage.list_requests(status="pending")[0]

    latest_step = PlanStep(
        index=0,
        intent="write current target",
        capability="filesystem.write",
        action="write",
        scope="filesystem:write",
        params={
            "path": "current.txt", "content": "current",
            "overwrite": False,
        },
        verification=VerificationPolicy("write_verified"),
    )
    manager.record_plan_version(
        goal_id, "direct", [latest_step.to_dict()],
        reason="replan_current_target",
    )
    recovery = MutationRecovery(
        recovery_id="recovery-preserved",
        task_id=old_task.id,
        goal_id=goal_id,
        step_index=0,
        capability="filesystem.write",
        action="write",
        resource="notes.txt",
        reason="operator decision still required",
    )
    storage.create_recovery(recovery)

    stale.scheduler_lease_seconds = 0.12
    stale_claim = stale._acquire_goal_run_lease(goal_id)
    assert stale_claim is not None
    stale._stop_lock_heartbeat(stale_claim[1])
    time.sleep(0.18)

    current, current_manager, current_store, _current_registry = _approval_engine(
        db, sandbox, max_wait=0
    )
    current.scheduler_lease_seconds = 0.12
    current_claim = current._acquire_goal_run_lease(goal_id)
    assert current_claim is not None
    before_task = storage.load_task(old_task.id)
    before_goal = manager.get_goal(goal_id)
    before_work = current_store.list_work(task_id=old_task.id)

    returned = stale._run_task_owned(
        old_task.id, goal_run_claim=stale_claim
    )

    after_task = storage.load_task(old_task.id)
    after_goal = manager.get_goal(goal_id)
    after_request = storage.get_request(old_request.approval_id)
    after_recovery = storage.get_recovery(recovery.recovery_id)
    assert returned.status is TaskStatus.AWAITING_APPROVAL
    assert after_task.status is TaskStatus.AWAITING_APPROVAL
    assert after_task.revision == before_task.revision
    assert after_task.error == before_task.error
    assert after_request.status is ApprovalStatus.PENDING
    assert after_request.decision_actor is None
    assert after_request.decided_at is None
    assert after_recovery.status is RecoveryStatus.REQUIRED
    assert after_recovery.acknowledged_by is None
    assert after_goal.status == before_goal.status
    assert after_goal.version == before_goal.version
    assert after_goal.blockers == before_goal.blockers
    assert current_store.list_work(task_id=old_task.id) == before_work

    current._release_goal_run_lease(goal_id, current_claim)
    stale._release_goal_run_lease(goal_id, (stale_claim[0], None))
    stale.shutdown(); current.shutdown()
    storage.close(); current_store.close()
    manager.cognitive_store.close(); current_manager.cognitive_store.close()
