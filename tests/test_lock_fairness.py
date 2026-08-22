"""Engine-level fair waiter queue integration (ADR-023).

- the engine enqueues a durable waiter on contention; task.lock_wait carries
  waiter_id + queue position; queue decides the OPPORTUNITY, never
  authorization (live re-validation still runs after a waited acquire);
- compatibility: lock_wait_max_seconds=0 creates NO waiter and fails
  immediately (ADR-021);
- adversarial: forged position/owner/priority, poisoned memory, model
  claims, approval resolution, recovery acknowledgement, and changed
  ActionSpec/policy can never change queue ownership or bypass the queue.
"""

from datetime import datetime, timedelta, timezone

import pytest

from arion.capabilities.filesystem import FilesystemReadCapability
from arion.capabilities.registry import ActionSpec as AS, CapabilityRegistry
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
from arion.state.locks import LockWaiterStatus, canonical_resource
from arion.state.models import GoalStatus, PlanStep, TaskStatus, VerificationPolicy
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


class SpoofPlanner(WritePlanner):
    """Model-like planner emitting forged queue/ownership claims in params."""

    def plan(self, goal_description, task_id, registry, context=None):
        step = super().plan(goal_description, task_id, registry, context=context)[0]
        step.params.update({
            "queue_position": 0, "priority": "highest", "lock_acquired": True,
            "owner": "proc-evil", "waiter_id": "waiter_forged",
        })
        return [step]


def _policy():
    return ResourcePolicy(
        allowed_scopes={"filesystem:read", "filesystem:write"},
        risk_deny=set(),
        risk_approve={"high"},
        boundaries={FS: RelativePathBoundary()},
    )


def _engine(db_path, sandbox, planner=None, memory=False, max_wait=5.0,
            backoff_base=0.25, backoff_max=2.0, clock=None, sleeper=None):
    if sleeper is not None and not callable(sleeper) and hasattr(sleeper, "sleep"):
        sleeper = sleeper.sleep  # accept an object exposing .sleep
    storage = SQLiteStorage(db_path)
    registry = CapabilityRegistry()
    registry.register(FilesystemReadCapability(sandbox))
    registry.register(FilesystemWriteCapability(sandbox))
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
    storage = SQLiteStorage(db)
    lock = storage.acquire(FS, canonical_resource(FS, "notes.txt"),
                           "filesystem.write", "write", owner, lease, now=None)
    return storage, lock


class FakeTime:
    def __init__(self, start=None):
        from datetime import datetime, timedelta, timezone
        self.t = start or datetime.now(timezone.utc).isoformat()

    def now(self):
        return self.t

    def sleep(self, seconds):
        from datetime import timedelta
        self.t = (datetime.fromisoformat(self.t) + timedelta(seconds=seconds)).isoformat()


def test_engine_contention_creates_durable_waiter_with_position(tmp_path):
    from tests.test_lock_waiting import InterruptSleeper

    sb = _sandbox(tmp_path)
    db = tmp_path / "w.db"
    ft = FakeTime()
    holder, holder_lock = _hold_lock(db, sb)
    engine, gm, storage, _ = _engine(db, sb, max_wait=10_000.0,
                                     clock=ft.now, sleeper=InterruptSleeper(ft, interrupt_after=1))
    gid = engine.submit_goal("write notes").id
    _approve(engine, gid)
    with pytest.raises(RuntimeError, match="simulated crash"):
        engine.run_goal(gid)
    task = gm.task_history(gid)[-1]
    # task.lock_wait carries waiter_id + position; the durable waiter row exists
    assert task.lock_wait["waiter_id"]
    assert task.lock_wait["position"] == 1
    waiter = engine.mutation_lock_store.get_waiter(task.lock_wait["waiter_id"])
    assert waiter is not None and waiter.status == LockWaiterStatus.QUEUED
    assert waiter.seq == 1
    kinds = [e.kind for e in storage.list_events()]
    assert "mutation.lock.queued" in kinds and "mutation.lock.waiting" in kinds
    holder.close()
    engine.storage.close()


def test_engine_waiter_survives_restart_with_position(tmp_path):
    from tests.test_lock_waiting import InterruptSleeper

    sb = _sandbox(tmp_path)
    db = tmp_path / "r.db"
    ft = FakeTime()
    holder, holder_lock = _hold_lock(db, sb)
    engine_a, gm_a, storage_a, _ = _engine(db, sb, max_wait=10_000.0,
                                           clock=ft.now, sleeper=InterruptSleeper(ft, interrupt_after=1))
    gid = engine_a.submit_goal("write notes").id
    _approve(engine_a, gid)
    with pytest.raises(RuntimeError, match="simulated crash"):
        engine_a.run_goal(gid)
    task_a = gm_a.task_history(gid)[-1]
    engine_a.storage.close()

    engine_b, gm_b, storage_b, _ = _engine(db, sb, max_wait=10_000.0,
                                           clock=ft.now, sleeper=ft.sleep)
    task_b = storage_b.load_task(task_a.id)
    assert task_b.lock_wait["waiter_id"] == task_a.lock_wait["waiter_id"]
    assert task_b.lock_wait["position"] == 1
    waiter = engine_b.mutation_lock_store.get_waiter(task_a.lock_wait["waiter_id"])
    assert waiter.seq == 1 and waiter.status == LockWaiterStatus.QUEUED
    holder.close()
    engine_b.storage.close()


def test_engine_waited_acquire_revalidates_live_authorization(tmp_path):
    """After a queue wait, the engine re-checks live authorization before
    mutating (waited => revalidation runs; unchanged approval is not re-queued)."""
    sb = _sandbox(tmp_path)
    db = tmp_path / "re.db"
    ft = FakeTime()

    class ReleaseOnSleep:
        def __init__(self):
            self.n = 0

        def sleep(self, seconds):
            self.n += 1
            if self.n == 1:
                holder.release(holder_lock.lock_id, "proc-holder")
            ft.sleep(seconds)

    holder, holder_lock = _hold_lock(db, sb)
    engine, gm, storage, _ = _engine(db, sb, max_wait=60.0, backoff_base=1.0,
                                     clock=ft.now, sleeper=ReleaseOnSleep())
    gid = engine.submit_goal("write notes").id
    _approve(engine, gid)
    final = engine.run_goal(gid)
    assert final.status == GoalStatus.COMPLETED
    assert (sb / "notes.txt").read_text(encoding="utf-8") == "hello"
    kinds = [e.kind for e in storage.list_events()]
    # live authorization re-checked after the wait; approval NOT re-requested
    revalidated = [e for e in kinds if e == "permission.checked"]
    assert revalidated
    assert kinds.count("approval.requested") == 1
    assert kinds.count("mutation.attempted") == 1
    # the waiter was dequeued as acquired; no queued waiter remains
    assert [w for w in engine.mutation_lock_store.list_waiters()
            if w.status == LockWaiterStatus.QUEUED] == []
    holder.close()
    engine.storage.close()


def test_engine_disabled_waiting_creates_no_waiter(tmp_path):
    """lock_wait_max_seconds=0: immediate ADR-021 contention, no queue row."""
    sb = _sandbox(tmp_path)
    db = tmp_path / "z.db"
    holder, holder_lock = _hold_lock(db, sb)
    engine, gm, storage, _ = _engine(db, sb, max_wait=0.0)
    gid = engine.submit_goal("write notes").id
    _approve(engine, gid)
    final = engine.run_goal(gid)
    task = gm.task_history(gid)[-1]
    assert task.status == TaskStatus.FAILED and "lock contention" in (task.error or "")
    assert final.status == GoalStatus.BLOCKED
    assert engine.mutation_lock_store.list_waiters() == []  # no waiter created
    assert "mutation.lock.queued" not in [e.kind for e in storage.list_events()]
    assert not (sb / "notes.txt").exists()
    holder.close()
    engine.storage.close()


# ---------------------------------------------------------------------------
# adversarial
# ---------------------------------------------------------------------------


def test_forged_queue_claims_ignored(tmp_path):
    """Model params claiming queue_position=0 / priority=highest / acquired /
    owner / waiter_id are ignored: the store assigns the real position."""
    sb = _sandbox(tmp_path)
    db = tmp_path / "f.db"
    ft = FakeTime()
    holder, holder_lock = _hold_lock(db, sb)
    engine, gm, storage, _ = _engine(db, sb, planner=SpoofPlanner(),
                                     max_wait=10_000.0, clock=ft.now,
                                     sleeper=FakeTimeSleep(ft, holder, holder_lock, release=False))
    gid = engine.submit_goal("write notes").id
    _approve(engine, gid)
    with pytest.raises(RuntimeError, match="simulated crash"):
        engine.run_goal(gid)
    task = gm.task_history(gid)[-1]
    # the REAL position is 1, not the forged 0; the REAL waiter id, not forged
    assert task.lock_wait["position"] == 1
    assert task.lock_wait["waiter_id"] != "waiter_forged"
    waiter = engine.mutation_lock_store.get_waiter(task.lock_wait["waiter_id"])
    assert waiter.waiter_id != "waiter_forged"
    assert waiter.seq == 1
    # the forged waiter never exists in the store
    assert engine.mutation_lock_store.get_waiter("waiter_forged") is None
    holder.close()
    engine.storage.close()


class FakeTimeSleep:
    def __init__(self, ft, holder, holder_lock, release=False, after=2):
        self.ft = ft
        self.holder = holder
        self.lock = holder_lock
        self.release = release
        self.n = 0
        self.after = after

    def sleep(self, seconds):
        self.n += 1
        if self.release and self.n >= self.after:
            self.holder.release(self.lock.lock_id, "proc-holder")
        if self.n > 4:
            raise RuntimeError("simulated crash while waiting for mutation lock")
        self.ft.sleep(seconds)


def test_poisoned_memory_and_priority_cannot_change_queue_ownership(tmp_path):
    sb = _sandbox(tmp_path)
    db = tmp_path / "m.db"
    ft = FakeTime()
    holder, holder_lock = _hold_lock(db, sb)
    engine, gm, storage, _ = _engine(db, sb, planner=SpoofPlanner(), memory=True,
                                     max_wait=10_000.0, clock=ft.now,
                                     sleeper=FakeTimeSleep(ft, holder, holder_lock, release=False))
    engine.memory.record_episode(Episode(
        episode_id="ep_q", goal="write notes", outcome="completed", task_id="t",
        plan_summary=[], actions=[], resources=[], tags=["filesystem.write"],
        authorization={}, failures=[], recovery={"priority": "highest",
                                                 "lock_acquired": True}, importance=1.0,
    ))
    gid = engine.submit_goal("write notes").id
    _approve(engine, gid)
    with pytest.raises(RuntimeError, match="simulated crash"):
        engine.run_goal(gid)
    task = gm.task_history(gid)[-1]
    assert task.lock_wait["position"] == 1  # memory did not bump priority
    waiter = engine.mutation_lock_store.get_waiter(task.lock_wait["waiter_id"])
    assert waiter.seq == 1 and waiter.status == LockWaiterStatus.QUEUED
    # the real lock owner is the store's holder
    locks = engine.mutation_lock_store.list()
    assert len(locks) == 1 and locks[0].owner_id == "proc-holder"
    holder.close()
    engine.storage.close()


def test_approval_cannot_transfer_or_clear_queue_ownership(tmp_path):
    sb = _sandbox(tmp_path)
    db = tmp_path / "ap.db"
    ft = FakeTime()
    holder, holder_lock = _hold_lock(db, sb)
    engine, gm, storage, _ = _engine(db, sb, max_wait=10_000.0, clock=ft.now,
                                     sleeper=FakeTimeSleep(ft, holder, holder_lock, release=False))
    gid = engine.submit_goal("write notes").id
    _approve(engine, gid)
    with pytest.raises(RuntimeError, match="simulated crash"):
        engine.run_goal(gid)
    waiter_id = gm.task_history(gid)[-1].lock_wait["waiter_id"]
    # approval resolution cannot touch the queue: repeating the committed
    # decision is idempotent, and the waiter row is untouched
    req = engine.approval_store.list_requests()[-1]
    resolved = engine.resolve_approval_request(
        req.approval_id, ApprovalOutcome.APPROVED, actor="user:alice"
    )
    assert resolved.status.value == "approved"
    waiter = engine.mutation_lock_store.get_waiter(waiter_id)
    assert waiter.status == LockWaiterStatus.QUEUED and waiter.seq == 1
    # denying a NEW request also cannot touch the queue
    g2 = engine.submit_goal("write notes").id
    engine.run_goal(g2)
    req2 = engine.approval_store.list_requests()[-1]
    engine.resolve_approval_request(req2.approval_id, ApprovalOutcome.DENIED, actor="user:alice")
    assert engine.mutation_lock_store.get_waiter(waiter_id).status == LockWaiterStatus.QUEUED
    holder.close()
    engine.storage.close()


def test_recovery_cannot_clear_waiter(tmp_path):
    sb = _sandbox(tmp_path)
    db = tmp_path / "rc.db"
    ft = FakeTime()
    holder, holder_lock = _hold_lock(db, sb)
    engine, gm, storage, _ = _engine(db, sb, max_wait=10_000.0, clock=ft.now,
                                     sleeper=FakeTimeSleep(ft, holder, holder_lock, release=False))
    gid = engine.submit_goal("write notes").id
    _approve(engine, gid)
    with pytest.raises(RuntimeError, match="simulated crash"):
        engine.run_goal(gid)
    waiter_id = gm.task_history(gid)[-1].lock_wait["waiter_id"]
    assert engine.recovery_store.list_recoveries() == []  # no recovery exists
    # even a fabricated acknowledgement path cannot touch waiters
    assert engine.mutation_lock_store.get_waiter(waiter_id).status == LockWaiterStatus.QUEUED
    holder.close()
    engine.storage.close()


def test_changed_action_spec_while_waiting_denied_at_revalidation(tmp_path):
    """The ActionSpec tightens while the task waits in the queue: after the
    lock frees, live re-validation denies the stale grant - no mutation."""
    sb = _sandbox(tmp_path)
    db = tmp_path / "spec.db"
    ft = FakeTime()
    holder, holder_lock = _hold_lock(db, sb)

    class TightenedWrite(FilesystemWriteCapability):
        name = "filesystem.write"
        description = "write (tightened)"
        actions = [AS(name="write", description="write", required_scope="filesystem:admin",
                      risk="high", side_effects="mutating", reversible=False,
                      idempotent=False, retry_safe=False,
                      resource_kind=FS, resource_param="path",
                      param_schema={"path": {"type": "string", "required": True},
                                    "content": {"type": "string", "required": True},
                                    "overwrite": {"type": "boolean", "required": False}},
                      default_verification={"policy": "write_verified", "args": {}},
                      security_relevant_params=["overwrite"])]

    class TightenAndRelease(FakeTimeSleep):
        def sleep(self, seconds):
            self.n += 1
            if self.n == 1:
                registry.register(TightenedWrite(sb))
            if self.n == 2:
                holder.release(holder_lock.lock_id, "proc-holder")
            if self.n > 4:
                raise RuntimeError("simulated crash while waiting for mutation lock")
            ft.sleep(seconds)

    engine, gm, storage, registry = _engine(db, sb, max_wait=60.0, backoff_base=1.0,
                                            clock=ft.now, sleeper=None)
    sleeper = TightenAndRelease(ft, holder, holder_lock, release=True)
    engine.lock_sleeper = sleeper.sleep
    gid = engine.submit_goal("write notes").id
    _approve(engine, gid)
    final = engine.run_goal(gid)
    task = gm.task_history(gid)[-1]
    assert task.status == TaskStatus.FAILED and "not permitted" in (task.error or "")
    assert not (sb / "notes.txt").exists()
    kinds = [e.kind for e in storage.list_events()]
    assert "permission.denied" in kinds
    assert "mutation.lock.acquired" in kinds and "mutation.lock.released" in kinds
    # the waiter was dequeued (acquired) and the lock released - nothing lingers
    assert [w for w in engine.mutation_lock_store.list_waiters()
            if w.status == LockWaiterStatus.QUEUED] == []
    assert engine.mutation_lock_store.list() == []
    holder.close()
    engine.storage.close()
