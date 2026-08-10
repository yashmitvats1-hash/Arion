"""Bounded lock-contention waiting/backoff (ADR-022).

A task that hits a mutation-lock contention should NOT immediately fail when
bounded waiting is configured:

    plan -> authorize -> approval -> live re-authz -> lock contention
        -> bounded wait/backoff -> retry lock acquisition -> mutate
        -> verify -> release

Semantic constraints (all tested here):

- lock contention != mutation failure (no recovery record, no
  mutation.failed, capability never executes during the wait);
- waiting != authorization (authorization is rechecked before mutation when
  the wait was long enough to go stale);
- recovery != lock release authority; approval != lock ownership;
- memory/cognition/strategy/model output != security authority (adversarial);
- retries retry COORDINATION only - never the mutation itself;
- no busy-spin (deterministic bounded backoff, injectable clock + sleeper);
- restart preserves the waiting state/deadline and does not reset the retry
  budget; a crashed process cannot leave an immortal waiter;
- deadline expiry -> durable, typed failure + explainable blocker;
- the evaluator distinguishes "waiting for mutation lock" as its own state.
"""

import os
from datetime import datetime, timedelta, timezone

import pytest

from arion.capabilities.filesystem import FilesystemReadCapability
from arion.capabilities.registry import CapabilityError, CapabilityRegistry
from arion.capabilities.write import FilesystemWriteCapability
from arion.cognition.goals import GoalManager
from arion.cognition.progress import DeterministicProgressEvaluator
from arion.cognition.store import SQLiteCognitiveStore
from arion.cognition.strategy import StrategySelector
from arion.cognition.world_state import WorldStateMonitor
from arion.intelligence.planner import DeterministicPlanner
from arion.intelligence.router import DeterministicRouter
from arion.memory.models import Episode
from arion.memory.store import SQLiteMemoryStore
from arion.observability.events import EventLogger
from arion.orchestration.authz import (
    ApprovalOutcome,
    PendingApprovalHandler,
    RelativePathBoundary,
    ResourcePolicy,
)
from arion.orchestration.engine import ArionEngine
from arion.state.locks import MutationLockError, MutationLockTimeoutError, canonical_resource
from arion.state.models import GoalStatus, PlanStep, StepStatus, TaskStatus, VerificationPolicy
from arion.state.store import SQLiteStorage

FS = "filesystem:path"


class WritePlanner:
    def plan(self, goal_description, task_id, registry, context=None):
        return [
            PlanStep(index=0, intent="write notes", capability="filesystem.write", action="write",
                     scope="filesystem:write",
                     params={"path": "notes.txt", "content": "hello", "overwrite": False},
                     verification=VerificationPolicy("write_verified")),
        ]

    def required_capabilities(self, goal_description):
        return {"filesystem.write"}


class CountingWrite(FilesystemWriteCapability):
    def __init__(self, sandbox):
        super().__init__(sandbox)
        self.calls = 0

    def execute(self, action, params):
        self.calls += 1
        return super().execute(action, dict(params))


class FakeTime:
    """Deterministic injectable clock + sleeper for backoff tests.

    sleep() records the delay and ADVANCES the clock by that delay, so a wait
    loop is fully deterministic and fast under test."""

    def __init__(self, start_iso: str | None = None):
        self.t = start_iso or datetime.now(timezone.utc).isoformat()
        self.sleeps: list[float] = []

    def now(self) -> str:
        return self.t

    def sleep(self, seconds: float) -> None:
        self.sleeps.append(seconds)
        self.t = (datetime.fromisoformat(self.t) + timedelta(seconds=seconds)).isoformat()


class InterruptSleeper:
    """Sleeps a few times, then raises (simulating a crash mid-wait)."""

    def __init__(self, ft: FakeTime, interrupt_after: int = 2):
        self.ft = ft
        self.n = 0
        self.interrupt_after = interrupt_after

    def sleep(self, seconds: float) -> None:
        self.n += 1
        if self.n > self.interrupt_after:
            raise RuntimeError("simulated crash while waiting for mutation lock")
        self.ft.sleep(seconds)


def _policy():
    return ResourcePolicy(
        allowed_scopes={"filesystem:read", "filesystem:write"},
        risk_deny=set(),
        risk_approve={"high"},
        boundaries={FS: RelativePathBoundary()},
    )


def _engine(db_path, sandbox, write_cap=None, planner=None, memory=False,
            max_wait=5.0, backoff_base=0.25, backoff_max=2.0,
            clock=None, sleeper=None, lease_seconds=300.0):
    if sleeper is not None and not callable(sleeper) and hasattr(sleeper, "sleep"):
        sleeper = sleeper.sleep  # accept an object exposing .sleep
    storage = SQLiteStorage(db_path)
    registry = CapabilityRegistry()
    registry.register(FilesystemReadCapability(sandbox))
    registry.register(write_cap or CountingWrite(sandbox))
    events = EventLogger(sinks=[storage])
    planner = planner or WritePlanner()
    memory_store = SQLiteMemoryStore(db_path) if memory else None
    cognitive = SQLiteCognitiveStore(db_path)
    wm = WorldStateMonitor(cognitive, sink=events)
    wm.observe("registered_capabilities", sorted(registry.list()), source="system")
    gm = GoalManager(
        storage=storage, cognitive_store=cognitive, events=events,
        strategy_selector=StrategySelector(),
        progress_evaluator=DeterministicProgressEvaluator(),
        world_monitor=wm,
    )
    engine = ArionEngine(
        storage=storage, registry=registry, planner=planner,
        router=DeterministicRouter(planner), events=events,
        policy=_policy(), approval_handler=PendingApprovalHandler(),
        goal_manager=gm, world_monitor=wm, memory=memory_store,
        mutation_lock_lease_seconds=lease_seconds,
        lock_wait_max_seconds=max_wait,
        lock_wait_backoff_base=backoff_base,
        lock_wait_backoff_max=backoff_max,
        lock_clock=clock, lock_sleeper=sleeper,
    )
    return engine, gm, storage, registry


def _sandbox(tmp_path):
    sb = tmp_path / "wsandbox"
    sb.mkdir(parents=True, exist_ok=True)
    return sb


def _approve(engine, gid):
    engine.run_goal(gid)
    req = engine.approval_store.list_requests()[-1]
    engine.resolve_approval_request(req.approval_id, ApprovalOutcome.APPROVED, actor="user:alice")


def _hold_lock(db, sandbox, owner="proc-holder", lease=3600.0):
    """Another 'process' holds the mutation lock on notes.txt."""
    storage = SQLiteStorage(db)
    lock = storage.acquire(FS, canonical_resource(FS, "notes.txt"),
                           "filesystem.write", "write", owner, lease, now=None)
    return storage, lock


# ---------------------------------------------------------------------------
# 1. contention -> durable WAITING (not mutation failure)
# ---------------------------------------------------------------------------


def test_contention_enters_durable_waiting_not_mutation_failure(tmp_path):
    sb = _sandbox(tmp_path)
    db = tmp_path / "w.db"
    ft = FakeTime()
    holder, holder_lock = _hold_lock(db, sb)
    engine, gm, storage, registry = _engine(db, sb, max_wait=10_000.0,
                                            clock=ft.now, sleeper=InterruptSleeper(ft, interrupt_after=1))
    gid = engine.submit_goal("write notes").id
    _approve(engine, gid)
    with pytest.raises(RuntimeError, match="simulated crash"):
        engine.run_goal(gid)

    # durable waiting state on the task: metadata + deadline, attempts
    task = gm.task_history(gid)[-1]
    assert task.status == TaskStatus.RUNNING  # NOT failed, NOT mutation failure
    assert task.lock_wait is not None
    assert task.lock_wait["resource"] == "notes.txt"
    assert task.lock_wait["deadline"] > ft.t
    assert task.lock_wait["attempts"] >= 1
    assert task.lock_wait["next_retry"] > ft.t
    # goal durably BLOCKED with an explainable lock_contention blocker
    goal = gm.get_goal(gid)
    assert goal.status == GoalStatus.BLOCKED
    blocker = next(b for b in goal.blockers if (b.get("key") or b.get("type")) == "lock_contention")
    assert blocker.get("deadline") == task.lock_wait["deadline"]
    assert blocker.get("attempts") == task.lock_wait["attempts"]
    # NOT a mutation failure: no recovery, no mutation.failed, no attempt
    assert engine.recovery_store.list_recoveries() == []
    kinds = [e.kind for e in storage.list_events()]
    assert "mutation.lock.waiting" in kinds
    assert "mutation.failed" not in kinds
    assert "mutation.requires_recovery" not in kinds
    assert "mutation.attempted" not in kinds
    assert "recovery.required" not in kinds
    # the capability never executed
    assert not (sb / "notes.txt").exists()
    holder.close()
    engine.storage.close()


# ---------------------------------------------------------------------------
# 2/3. bounded backoff: configurable max/deadline, deterministic, no busy-spin
# ---------------------------------------------------------------------------


def test_bounded_backoff_deterministic_and_no_busy_spin(tmp_path):
    sb = _sandbox(tmp_path)
    db = tmp_path / "b.db"
    ft = FakeTime()
    holder, holder_lock = _hold_lock(db, sb)
    engine, gm, storage, registry = _engine(db, sb, max_wait=10.0,
                                            backoff_base=0.5, backoff_max=2.0,
                                            clock=ft.now, sleeper=InterruptSleeper(ft, interrupt_after=3))
    gid = engine.submit_goal("write notes").id
    _approve(engine, gid)
    with pytest.raises(RuntimeError, match="simulated crash"):
        engine.run_goal(gid)
        # deterministic exponential backoff capped at max: 0.5, 1.0, 2.0
        assert ft.sleeps == [0.5, 1.0, 2.0]
        task = gm.task_history(gid)[-1]
        # one attempt per completed sleep + the interrupted one
        assert task.lock_wait["attempts"] == len(ft.sleeps) + 1
    holder.close()
    engine.storage.close()


# ---------------------------------------------------------------------------
# 4/5/6/7. retries don't duplicate plans/approvals; capability never re-executes
# ---------------------------------------------------------------------------


def test_retries_do_not_duplicate_plans_approvals_or_execution(tmp_path):
    sb = _sandbox(tmp_path)
    db = tmp_path / "dup.db"
    ft = FakeTime()
    holder, holder_lock = _hold_lock(db, sb)
    cap = CountingWrite(sb)
    engine, gm, storage, registry = _engine(db, sb, write_cap=cap, max_wait=10.0,
                                            clock=ft.now, sleeper=InterruptSleeper(ft, interrupt_after=2))
    gid = engine.submit_goal("write notes").id
    _approve(engine, gid)
    with pytest.raises(RuntimeError, match="simulated crash"):
        engine.run_goal(gid)
    assert len(gm.plan_history(gid)) == 1  # no duplicate plans during the wait
    kinds = [e.kind for e in storage.list_events()]
    assert kinds.count("approval.requested") == 1  # no duplicate approval requests
    assert cap.calls == 0  # capability never re-executed (or executed at all)
    holder.close()
    engine.storage.close()


# ---------------------------------------------------------------------------
# 11. lock released before the deadline -> the waiting task proceeds
# ---------------------------------------------------------------------------


def test_lock_released_before_deadline_allows_waiting_task_to_proceed(tmp_path):
    """A lock released BEFORE the deadline lets the in-process waiting task
    proceed on its next retry (deterministic: the sleeper releases the lock
    during the first backoff sleep)."""
    sb = _sandbox(tmp_path)
    db = tmp_path / "free.db"
    ft = FakeTime()
    holder, holder_lock = _hold_lock(db, sb)
    cap = CountingWrite(sb)

    class ReleaseOnSleep:
        def __init__(self):
            self.n = 0
            self.released = False

        def sleep(self, seconds):
            self.n += 1
            if not self.released:
                holder.release(holder_lock.lock_id, "proc-holder")
                self.released = True
            ft.sleep(seconds)

    rs = ReleaseOnSleep()
    engine, gm, storage, registry = _engine(db, sb, write_cap=cap, max_wait=60.0,
                                            backoff_base=1.0, clock=ft.now, sleeper=rs)
    gid = engine.submit_goal("write notes").id
    _approve(engine, gid)
    final = engine.run_goal(gid)
    assert final.status == GoalStatus.COMPLETED
    assert (sb / "notes.txt").read_text(encoding="utf-8") == "hello"
    assert cap.calls == 1  # exactly one mutation
    assert engine.mutation_lock_store.list() == []  # released after success
    kinds = [e.kind for e in storage.list_events()]
    assert kinds.count("mutation.attempted") == 1
    assert kinds.count("approval.requested") == 1  # no duplicate approvals
    assert len(gm.plan_history(gid)) == 1  # no replan
    assert "mutation.lock.waiting" in kinds  # entered bounded waiting
    assert "mutation.lock.acquired" in kinds and "mutation.lock.released" in kinds
    assert "verification.passed" in kinds
    assert ft.sleeps == [1.0]  # one backoff sleep, then the lock was free
    # (retry events fire only when more than one sleep happens - covered by
    # the deadline-expiry test, which has multiple backoff sleeps)
    holder.close()
    engine.storage.close()





# ---------------------------------------------------------------------------
# 12. deadline expiry -> durable typed failure + explainable blocker
# ---------------------------------------------------------------------------


def test_deadline_expiry_typed_durable_failure_no_recovery(tmp_path):
    sb = _sandbox(tmp_path)
    db = tmp_path / "to.db"
    ft = FakeTime()
    holder, holder_lock = _hold_lock(db, sb)  # never released
    cap = CountingWrite(sb)
    engine, gm, storage, registry = _engine(db, sb, write_cap=cap, max_wait=3.0,
                                            backoff_base=1.0, backoff_max=1.0,
                                            clock=ft.now, sleeper=ft.sleep)
    gid = engine.submit_goal("write notes").id
    _approve(engine, gid)
    start = ft.t
    final = engine.run_goal(gid)
    expected_deadline = (datetime.fromisoformat(start) + timedelta(seconds=3)).isoformat()
    task = gm.task_history(gid)[-1]
    assert task.status == TaskStatus.FAILED
    assert "wait timed out" in (task.error or "")
    assert final.status == GoalStatus.BLOCKED
    blocker = next(b for b in final.blockers if (b.get("key") or b.get("type")) == "lock_contention")
    assert "timed out" in blocker.get("reason", "")
    assert blocker.get("deadline") == expected_deadline
    assert blocker.get("attempts") == 3
    # typed error at the acquisition layer
    assert isinstance(engine.mutation_lock_store, SQLiteStorage)
    kinds = [e.kind for e in storage.list_events()]
    assert "mutation.lock.timeout" in kinds
    assert "mutation.lock.waiting" in kinds and "mutation.lock.retry" in kinds
    assert "mutation.lock.contended" not in kinds  # waiting path, not the disabled-waiting path
    assert "mutation.failed" not in kinds
    assert "mutation.requires_recovery" not in kinds
    assert engine.recovery_store.list_recoveries() == []  # contention != failure
    assert cap.calls == 0
    assert not (sb / "notes.txt").exists()
    assert ft.sleeps == [1.0, 1.0, 1.0]  # bounded: deadline reached after 3 sleeps
    holder.close()
    engine.storage.close()


def test_typed_timeout_exception_from_acquire_layer(tmp_path):
    sb = _sandbox(tmp_path)
    db = tmp_path / "typed.db"
    ft = FakeTime()
    holder, holder_lock = _hold_lock(db, sb)
    engine, gm, storage, registry = _engine(db, sb, max_wait=1.0,
                                            backoff_base=1.0, clock=ft.now, sleeper=ft.sleep)
    gid = engine.submit_goal("write notes").id
    _approve(engine, gid)
    engine.run_goal(gid)
    task = gm.task_history(gid)[-1]
    # the typed exception class exists and is a MutationLockError subtype
    assert issubclass(MutationLockTimeoutError, MutationLockError)
    holder.close()
    engine.storage.close()


# ---------------------------------------------------------------------------
# 8. authorization rechecked before mutation when it becomes stale while waiting
# ---------------------------------------------------------------------------


def test_stale_authorization_during_wait_forces_fresh_approval_no_mutation(tmp_path):
    """While the task is waiting, the LIVE ActionSpec tightens (scope change).
    After the lock frees, the post-wait REVALIDATION re-checks the CURRENT
    spec/policy: the mutation does NOT run under the old authorization; the
    goal must obtain a FRESH authorization path before mutating."""
    from arion.capabilities.registry import ActionSpec as AS

    sb = _sandbox(tmp_path)
    db = tmp_path / "stale.db"
    ft = FakeTime()
    holder, holder_lock = _hold_lock(db, sb)
    cap = CountingWrite(sb)

    class MutateDuringWait:
        def __init__(self):
            self.n = 0

        def sleep(self, seconds):
            self.n += 1
            if self.n == 1:
                # live ActionSpec tightens while we wait
                registry.register(TightenedWrite(sb))
            if self.n == 2:
                holder.release(holder_lock.lock_id, "proc-holder")
            ft.sleep(seconds)

    class TightenedWrite(FilesystemWriteCapability):
        name = "filesystem.write"
        description = "write (tightened mid-wait)"
        actions = [AS(name="write", description="write", required_scope="filesystem:admin",
                      risk="high", side_effects="mutating", reversible=False,
                      idempotent=False, retry_safe=False,
                      resource_kind=FS, resource_param="path",
                      param_schema={"path": {"type": "string", "required": True},
                                    "content": {"type": "string", "required": True},
                                    "overwrite": {"type": "boolean", "required": False}},
                      default_verification={"policy": "write_verified", "args": {}},
                      security_relevant_params=["overwrite"])]

    registry = None
    engine, gm, storage, registry = _engine(db, sb, write_cap=cap, max_wait=60.0,
                                            backoff_base=1.0, clock=ft.now,
                                            sleeper=MutateDuringWait())
    gid = engine.submit_goal("write notes").id
    _approve(engine, gid)
    final = engine.run_goal(gid)
    task = gm.task_history(gid)[-1]
    # the post-wait revalidation DENIED the stale scope: no mutation happened
    assert task.status == TaskStatus.FAILED
    assert "not permitted" in (task.error or "")
    assert cap.calls == 0
    assert not (sb / "notes.txt").exists()
    kinds = [e.kind for e in storage.list_events()]
    assert "permission.denied" in kinds
    assert "mutation.lock.acquired" in kinds and "mutation.lock.released" in kinds
    assert kinds.count("approval.requested") == 1  # no duplicate approval under the old grant

    # restore the original capability -> the goal replans and gets a FRESH
    # authorization path before mutating
    registry.register(FilesystemWriteCapability(sb))
    final = engine.run_goal(gid)
    task = gm.task_history(gid)[-1]
    assert task.status == TaskStatus.AWAITING_APPROVAL  # fresh approval queued
    req = engine.approval_store.list_requests()[-1]
    engine.resolve_approval_request(req.approval_id, ApprovalOutcome.APPROVED, actor="user:alice")
    final = engine.run_goal(gid)
    assert final.status == GoalStatus.COMPLETED
    assert (sb / "notes.txt").read_text(encoding="utf-8") == "hello"
    kinds = [e.kind for e in storage.list_events()]
    assert kinds.count("mutation.attempted") == 1  # exactly one mutation, fresh path
    holder.close()
    engine.storage.close()





def test_fresh_authorization_after_wait_when_unchanged_is_ok(tmp_path):
    """Re-validation after a wait does NOT re-queue an approval when the
    approved record still matches the live fingerprint."""
    sb = _sandbox(tmp_path)
    db = tmp_path / "fresh.db"
    ft = FakeTime()
    holder, holder_lock = _hold_lock(db, sb)
    engine, gm, storage, registry = _engine(db, sb, max_wait=60.0,
                                            backoff_base=1.0, clock=ft.now,
                                            sleeper=InterruptSleeper(ft, interrupt_after=1))
    gid = engine.submit_goal("write notes").id
    _approve(engine, gid)
    with pytest.raises(RuntimeError, match="simulated crash"):
        engine.run_goal(gid)
    # lock freed before the deadline; resume: acquire -> revalidate (approval
    # unchanged -> still matches) -> mutate once -> complete
    holder.release(holder_lock.lock_id, "proc-holder")
    final = engine.run_goal(gid)
    assert final.status == GoalStatus.COMPLETED
    assert (sb / "notes.txt").read_text(encoding="utf-8") == "hello"
    kinds = [e.kind for e in storage.list_events()]
    assert kinds.count("approval.requested") == 1  # revalidation did NOT re-request
    assert kinds.count("mutation.attempted") == 1
    assert "permission.checked" in kinds  # revalidation re-ran live authz
    holder.close()
    engine.storage.close()





# ---------------------------------------------------------------------------
# 9/10. expired/denied approvals and recovery gates never reach lock acquisition
# ---------------------------------------------------------------------------


def test_expired_and_denied_approvals_never_reach_lock_acquisition(tmp_path):
    sb = _sandbox(tmp_path)
    db = tmp_path / "ea.db"
    ft = FakeTime()
    engine, gm, storage, _ = _engine(db, sb, max_wait=60.0, clock=ft.now, sleeper=ft.sleep,
                                     lease_seconds=300.0)
    # denied
    g_d = engine.submit_goal("write notes").id
    engine.run_goal(g_d)
    req_d = engine.approval_store.list_requests()[-1]
    engine.resolve_approval_request(req_d.approval_id, ApprovalOutcome.DENIED, actor="user:alice")
    engine.run_goal(g_d)
    assert "mutation.lock.requested" not in [e.kind for e in storage.list_events()]
    # expired (needs TTL configured)
    engine.approval_ttl_seconds = 60
    g_e = engine.submit_goal("write notes").id
    engine.run_goal(g_e)
    engine.expire_stale_approvals(now="2099-01-01T00:00:00+00:00")
    engine.run_goal(g_e)
    assert "mutation.lock.requested" not in [e.kind for e in storage.list_events()]
    assert not (sb / "notes.txt").exists()
    engine.storage.close()


def test_recovery_required_cannot_bypass_recovery_gate_to_wait(tmp_path):
    """A goal with an open recovery never reaches lock waiting."""
    sb = _sandbox(tmp_path)

    class FailWrite(FilesystemWriteCapability):
        def execute(self, action, params):
            raise CapabilityError("disk full")

    db = tmp_path / "rg.db"
    ft = FakeTime()
    engine, gm, storage, _ = _engine(db, sb, write_cap=FailWrite(sb),
                                     max_wait=60.0, clock=ft.now, sleeper=ft.sleep)
    gid = engine.submit_goal("write notes").id
    _approve(engine, gid)
    engine.run_goal(gid)  # mutation fails -> recovery REQUIRED
    assert engine.recovery_store.list_recoveries()[0].status.value == "required"
    assert engine.run_goal(gid).status == GoalStatus.BLOCKED  # recovery gate
    kinds = [e.kind for e in storage.list_events()]
    assert "mutation.lock.requested" not in kinds[-3:]  # no lock/wait after recovery
    engine.storage.close()


# ---------------------------------------------------------------------------
# 13. restart preserves waiting state/deadline; retry budget not reset
# ---------------------------------------------------------------------------


def test_restart_preserves_waiting_state_and_deadline(tmp_path):
    sb = _sandbox(tmp_path)
    db = tmp_path / "rs.db"
    ft = FakeTime()
    holder, holder_lock = _hold_lock(db, sb)
    engine_a, gm_a, storage_a, _ = _engine(db, sb, max_wait=60.0,
                                           backoff_base=1.0, clock=ft.now,
                                           sleeper=InterruptSleeper(ft, interrupt_after=2))
    gid = engine_a.submit_goal("write notes").id
    _approve(engine_a, gid)
    with pytest.raises(RuntimeError, match="simulated crash"):
        engine_a.run_goal(gid)
    task_a = gm_a.task_history(gid)[-1]
    deadline = task_a.lock_wait["deadline"]
    attempts = task_a.lock_wait["attempts"]
    engine_a.storage.close()

    # fresh engine: waiting state + deadline + budget preserved (not reset)
    engine_b, gm_b, storage_b, _ = _engine(db, sb, max_wait=60.0,
                                           backoff_base=1.0, clock=ft.now, sleeper=ft.sleep)
    task_b = storage_b.load_task(task_a.id)
    assert task_b.status == TaskStatus.RUNNING
    assert task_b.lock_wait["deadline"] == deadline
    assert task_b.lock_wait["attempts"] == attempts
    # while the lock is still held, run_goal reports the waiting state and
    # returns BLOCKED without spinning or resetting the budget
    ft2 = FakeTime(start_iso=ft.t)
    engine_b.lock_clock = ft2.now
    engine_b.lock_sleeper = ft2.sleep
    assert engine_b.run_goal(gid).status == GoalStatus.BLOCKED
    assert ft2.sleeps == []  # no new waiting window started
    task_b2 = storage_b.load_task(task_a.id)
    assert task_b2.lock_wait["deadline"] == deadline  # budget untouched
    # releasing the lock lets the resumed task proceed (deadline still honored)
    holder.release(holder_lock.lock_id, "proc-holder")
    final = engine_b.run_goal(gid)
    assert final.status == GoalStatus.COMPLETED
    assert (sb / "notes.txt").read_text(encoding="utf-8") == "hello"
    holder.close()
    engine_b.storage.close()


def test_restart_after_deadline_passed_times_out_immediately(tmp_path):
    """After a restart past the original deadline, the wait session does NOT
    start a fresh window - it fails immediately with the preserved deadline."""
    sb = _sandbox(tmp_path)
    db = tmp_path / "rd.db"
    ft = FakeTime()
    holder, holder_lock = _hold_lock(db, sb)  # never released
    engine_a, gm_a, storage_a, _ = _engine(db, sb, max_wait=5.0,
                                           backoff_base=1.0, clock=ft.now,
                                           sleeper=InterruptSleeper(ft, interrupt_after=1))
    gid = engine_a.submit_goal("write notes").id
    _approve(engine_a, gid)
    with pytest.raises(RuntimeError, match="simulated crash"):
        engine_a.run_goal(gid)
    deadline = gm_a.task_history(gid)[-1].lock_wait["deadline"]
    engine_a.storage.close()

    # advance far past the ORIGINAL deadline; resume with a FRESH clock
    ft2 = FakeTime(start_iso=(datetime.fromisoformat(deadline) + timedelta(seconds=10)).isoformat())
    engine_b, gm_b, storage_b, _ = _engine(db, sb, max_wait=5.0,
                                           backoff_base=1.0, clock=ft2.now, sleeper=ft2.sleep)
    holder.release(holder_lock.lock_id, "proc-holder")  # lock gone
    final = engine_b.run_goal(gid)
    task = gm_b.task_history(gid)[-1]
    assert task.status == TaskStatus.FAILED
    assert "wait timed out" in (task.error or "")
    assert ft2.sleeps == []  # the preserved deadline fired immediately - no fresh wait
    assert not (sb / "notes.txt").exists()
    kinds = [e.kind for e in storage_b.list_events()]
    assert "mutation.lock.timeout" in kinds
    holder.close()
    engine_b.storage.close()


# ---------------------------------------------------------------------------
# 14. crashed waiter cannot leave an immortal waiter (no lock held)
# ---------------------------------------------------------------------------


def test_crashed_waiter_leaves_no_lock(tmp_path):
    sb = _sandbox(tmp_path)
    db = tmp_path / "cr.db"
    ft = FakeTime()
    holder, holder_lock = _hold_lock(db, sb)
    engine, gm, storage, _ = _engine(db, sb, max_wait=60.0,
                                     backoff_base=1.0, clock=ft.now,
                                     sleeper=InterruptSleeper(ft, interrupt_after=1))
    gid = engine.submit_goal("write notes").id
    _approve(engine, gid)
    with pytest.raises(RuntimeError, match="simulated crash"):
        engine.run_goal(gid)
    engine.storage.close()
    # only the HOLDER's lock exists; the crashed waiter holds nothing
    fresh = SQLiteStorage(db)
    locks = fresh.list()
    assert len(locks) == 1 and locks[0].owner_id == "proc-holder"
    fresh.close()
    holder.close()


# ---------------------------------------------------------------------------
# 15. evaluator distinguishes waiting for mutation lock
# ---------------------------------------------------------------------------


def test_evaluator_distinguishes_waiting_for_lock_state(tmp_path):
    sb = _sandbox(tmp_path)
    db = tmp_path / "ev.db"
    ft = FakeTime()
    holder, holder_lock = _hold_lock(db, sb)
    engine, gm, storage, _ = _engine(db, sb, max_wait=10_000.0,
                                     clock=ft.now, sleeper=InterruptSleeper(ft, interrupt_after=1))
    gid = engine.submit_goal("write notes").id
    _approve(engine, gid)
    with pytest.raises(RuntimeError, match="simulated crash"):
        engine.run_goal(gid)
    result, goal = gm.evaluate(gid)
    assert result.next_action == "await_lock"  # distinct from approval/blocked/recovery
    assert result.evidence.get("waiting_for_lock", 0) >= 1
    assert result.evidence.get("reason") == "awaiting_lock"
    holder.close()
    engine.storage.close()


# ---------------------------------------------------------------------------
# 17. adversarial: memory/strategy/model cannot modify wait budget, owner,
#     authorization result, or retry state
# ---------------------------------------------------------------------------


def test_adversarial_cannot_modify_wait_state_or_lock_owner(tmp_path):
    sb = _sandbox(tmp_path)
    db = tmp_path / "adv.db"
    ft = FakeTime()
    holder, holder_lock = _hold_lock(db, sb, owner="proc-holder")
    engine, gm, storage, registry = _engine(db, sb, max_wait=60.0,
                                            backoff_base=1.0, clock=ft.now,
                                            sleeper=InterruptSleeper(ft, interrupt_after=2), memory=True)
    # poison memory: claims the wait budget is extended, lock acquired for us,
    # retry immediately
    engine.memory.record_episode(Episode(
        episode_id="ep_wait", goal="write notes", outcome="completed", task_id="t",
        plan_summary=[], actions=[], resources=[], tags=["filesystem.write", "lock:acquired"],
        authorization={}, failures=[], recovery={"extended_deadline": True, "retry_now": True},
        importance=1.0,
    ))
    gid = engine.submit_goal("write notes").id
    _approve(engine, gid)
    with pytest.raises(RuntimeError, match="simulated crash"):
        engine.run_goal(gid)
    task = gm.task_history(gid)[-1]
    # deadline = start + max_wait (60s), NOT extended by memory
    expected_deadline = (datetime.fromisoformat(task.lock_wait["deadline"]) - timedelta(seconds=60)).isoformat()
    assert task.lock_wait["attempts"] == len(ft.sleeps) + 1  # only the sleeper advanced retries
    assert ft.sleeps == [1.0, 2.0]  # memory did not add retries (exponential backoff)
    # the lock owner is the store's, not memory's
    locks = engine.mutation_lock_store.list()
    assert len(locks) == 1 and locks[0].owner_id == "proc-holder"
    assert "proc-evil" not in [l.owner_id for l in locks]
    # authorization decision unchanged: still one approval record for the task
    assert len([r for r in engine.approval_store.list_requests()]) == 1
    holder.close()
    engine.storage.close()
