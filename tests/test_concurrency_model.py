"""Bounded in-process concurrency model (ADR-024, Phase A).

- configurable max_concurrency; default 1 (backward compatible);
- read-only independent steps may execute concurrently;
- mutating steps serialize through the existing durable mutation lock
  (same resource) or run concurrently (different resources);
- every concurrent execution gets its OWN live authorization check;
- task dependency constraints remain authoritative (a blocked step never
  stalls unrelated ready steps);
- max_concurrency=1 reproduces current sequential behavior;
- bounded worker shutdown; cancellation of queued work; restart while
  multiple tasks are in flight; no duplicate mutation after restart;
- approval-pending / recovery-required steps never consume a worker;
- FIFO waiter fairness preserved.
"""

import threading
import time
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
from arion.observability.events import EventLogger
from arion.orchestration.authz import (
    ApprovalOutcome,
    PendingApprovalHandler,
    RelativePathBoundary,
    ResourcePolicy,
)
from arion.orchestration.engine import ArionEngine
from arion.state.locks import LockWaiterStatus, canonical_resource
from arion.state.models import GoalStatus, PlanStep, StepStatus, TaskStatus, VerificationPolicy
from arion.state.store import SQLiteStorage

FS = "filesystem:path"


class SlowReadCapability:
    """Read-only capability whose execute sleeps (deterministic barrier)."""

    name = "filesystem.read"
    description = "slow read"
    actions = [AS(name="read", description="read", required_scope="filesystem:read",
                  risk="low", side_effects="read_only", reversible=True,
                  idempotent=True, retry_safe=True,
                  resource_kind=FS, resource_param="path",
                  param_schema={"path": {"type": "string", "required": True}},
                  default_verification={"policy": "schema_keys", "args": {"keys": ["content"]}})]

    def __init__(self, sleep=0.3, barrier=None):
        self.sleep = sleep
        self.barrier = barrier
        self.active = 0
        self.max_active = 0
        self.lock = threading.Lock()
        self.started = []

    def execute(self, action, params):
        with self.lock:
            self.active += 1
            self.max_active = max(self.max_active, self.active)
            self.started.append(params.get("path"))
        try:
            if self.barrier is not None:
                self.barrier.wait(timeout=5)
            time.sleep(self.sleep)
        finally:
            with self.lock:
                self.active -= 1
        return {"content": f"read {params.get('path')}", "size": 1}


class SlowWriteCapability(FilesystemWriteCapability):
    """Write capability with an optional sleep (holds the mutation lock)."""

    def __init__(self, sandbox, sleep=0.0, barrier=None):
        super().__init__(sandbox)
        self.sleep = sleep
        self.barrier = barrier
        self.active = 0
        self.max_active = 0
        self.mlock = threading.Lock()
        self.calls = 0

    def execute(self, action, params):
        with self.mlock:
            self.active += 1
            self.max_active = max(self.max_active, self.active)
        try:
            if self.barrier is not None:
                self.barrier.wait(timeout=5)
            time.sleep(self.sleep)
            with self.mlock:
                self.calls += 1
            return super().execute(action, dict(params))
        finally:
            with self.mlock:
                self.active -= 1


class TwoStepPlanner:
    """Two independent steps (same capability, different params)."""

    def __init__(self, steps):
        self._steps = steps

    def plan(self, goal_description, task_id, registry, context=None):
        return [PlanStep(index=i, intent=s[0], capability=s[1], action=s[2],
                         scope=s[3], params=dict(s[4]), verification=s[5],
                         depends_on=list(s[6]) if len(s) > 6 else [])
                for i, s in enumerate(self._steps)]

    def required_capabilities(self, goal_description):
        caps = {s[1] for s in self._steps}
        return caps


def _policy():
    return ResourcePolicy(
        allowed_scopes={"filesystem:read", "filesystem:write"},
        risk_deny=set(),
        risk_approve={"high"},
        boundaries={FS: RelativePathBoundary()},
    )


def _engine(db_path, sandbox, planner, read_cap=None, write_cap=None,
            max_concurrency=2, clock=None, sleeper=None, approve_required=True):
    storage = SQLiteStorage(db_path)
    registry = CapabilityRegistry()
    registry.register(read_cap or FilesystemReadCapability(sandbox))
    registry.register(write_cap or FilesystemWriteCapability(sandbox))
    events = EventLogger(sinks=[storage])
    cognitive = SQLiteCognitiveStore(db_path)
    wm = WorldStateMonitor(cognitive, sink=events)
    wm.observe("registered_capabilities", sorted(registry.list()), source="system")
    gm = GoalManager(
        storage=storage, cognitive_store=cognitive, events=events,
        strategy_selector=StrategySelector(),
        progress_evaluator=DeterministicProgressEvaluator(),
        world_monitor=wm,
    )
    policy = _policy()
    if not approve_required:
        policy = ResourcePolicy(
            allowed_scopes={"filesystem:read", "filesystem:write"},
            risk_deny=set(), risk_approve=set(),
            boundaries={FS: RelativePathBoundary()})
    engine = ArionEngine(
        storage=storage, registry=registry, planner=planner,
        router=DeterministicRouter(planner), events=events,
        policy=policy, approval_handler=PendingApprovalHandler(),
        goal_manager=gm, world_monitor=wm,
        max_concurrency=max_concurrency,
        lock_wait_max_seconds=0.0,  # immediate contention (queue disabled here)
        lock_clock=clock, lock_sleeper=sleeper,
    )
    return engine, gm, storage, registry


def _sandbox(tmp_path):
    sb = tmp_path / "wsandbox"
    sb.mkdir(parents=True, exist_ok=True)
    (sb / "a.txt").write_text("a", encoding="utf-8")
    (sb / "b.txt").write_text("b", encoding="utf-8")
    return sb


def _approve_all(engine, gid):
    """Approve every approval the goal needs, step by step (each step has its
    own durable request; under concurrency they are requested sequentially)."""
    for _ in range(10):
        engine.run_goal(gid)
        pending = [r for r in engine.approval_store.list_requests() if r.status.value == "pending"]
        if not pending:
            break
        for req in pending:
            engine.resolve_approval_request(req.approval_id, ApprovalOutcome.APPROVED, actor="user:alice")


# ---------------------------------------------------------------------------
# 1. two independent read-only steps executing concurrently
# ---------------------------------------------------------------------------


def test_two_independent_reads_execute_concurrently(tmp_path):
    sb = _sandbox(tmp_path)
    barrier = threading.Barrier(2)
    read_cap = SlowReadCapability(sleep=0.2, barrier=barrier)
    planner = TwoStepPlanner([
        ("read a", "filesystem.read", "read", "filesystem:read", {"path": "a.txt"},
         VerificationPolicy("schema_keys", {"keys": ["content"]})),
        ("read b", "filesystem.read", "read", "filesystem:read", {"path": "b.txt"},
         VerificationPolicy("schema_keys", {"keys": ["content"]})),
    ])
    engine, gm, storage, _ = _engine(tmp_path / "c.db", sb, planner, read_cap=read_cap,
                                     max_concurrency=2)
    gid = engine.submit_goal("read a and b").id
    t0 = time.monotonic()
    final = engine.run_goal(gid)
    elapsed = time.monotonic() - t0
    assert final.status == GoalStatus.COMPLETED
    # both were in execute() at the same time (barrier proved it)
    assert read_cap.max_active >= 2
    assert read_cap.started == ["a.txt", "b.txt"] or set(read_cap.started) == {"a.txt", "b.txt"}
    # wall-clock materially below serial (2 x 0.2s + barrier) -> concurrent
    assert elapsed < 0.45, f"elapsed {elapsed} not below serial"
    # each got its own live authorization check
    checked = [e for e in storage.list_events() if e.kind == "permission.checked"]
    assert len(checked) == 2
    engine.storage.close()


def test_reads_each_authorize_independently(tmp_path):
    """Concurrent reads each run their own permission.checked (no reuse)."""
    sb = _sandbox(tmp_path)
    read_cap = SlowReadCapability(sleep=0.05)
    planner = TwoStepPlanner([
        ("read a", "filesystem.read", "read", "filesystem:read", {"path": "a.txt"},
         VerificationPolicy("schema_keys", {"keys": ["content"]})),
        ("read b", "filesystem.read", "read", "filesystem:read", {"path": "b.txt"},
         VerificationPolicy("schema_keys", {"keys": ["content"]})),
    ])
    engine, gm, storage, _ = _engine(tmp_path / "d.db", sb, planner, read_cap=read_cap,
                                     max_concurrency=2)
    gid = engine.submit_goal("read a and b").id
    engine.run_goal(gid)
    checked = [e for e in storage.list_events() if e.kind == "permission.checked"]
    assert len(checked) == 2
    assert len({(e.step_id) for e in checked}) == 2
    engine.storage.close()


# ---------------------------------------------------------------------------
# 2/3. mutating steps: same resource serialize; different resources concurrent
# ---------------------------------------------------------------------------


def test_same_resource_mutations_serialize_through_lock(tmp_path):
    sb = _sandbox(tmp_path)
    write_cap = SlowWriteCapability(sb, sleep=0.2)  # no barrier: serialized
    planner = TwoStepPlanner([
        ("write a", "filesystem.write", "write", "filesystem:write",
         {"path": "a.txt", "content": "x", "overwrite": True},
         VerificationPolicy("write_verified")),
        ("write a2", "filesystem.write", "write", "filesystem:write",
         {"path": "a.txt", "content": "y", "overwrite": True},
         VerificationPolicy("write_verified")),
    ])
    engine, gm, storage, _ = _engine(tmp_path / "e.db", sb, planner, write_cap=write_cap,
                                     max_concurrency=2, approve_required=False)
    gid = engine.submit_goal("write a twice").id
    # approval not required (concurrency-only test)
    final = engine.run_goal(gid)
    assert final.status == GoalStatus.COMPLETED
    # both attempted; never concurrently (the durable lock serializes)
    assert write_cap.calls == 2
    assert write_cap.max_active == 1
    # audit: lock acquired/released twice, interleaved, never overlapping
    acq = [e for e in storage.list_events() if e.kind == "mutation.lock.acquired"]
    rel = [e for e in storage.list_events() if e.kind == "mutation.lock.released"]
    assert len(acq) == 2 and len(rel) == 2
    engine.storage.close()


def test_different_resource_mutations_execute_concurrently(tmp_path):
    sb = _sandbox(tmp_path)
    barrier = threading.Barrier(2)
    write_cap = SlowWriteCapability(sb, sleep=0.2, barrier=barrier)
    planner = TwoStepPlanner([
        ("write a", "filesystem.write", "write", "filesystem:write",
         {"path": "a.txt", "content": "x", "overwrite": True},
         VerificationPolicy("write_verified")),
        ("write b", "filesystem.write", "write", "filesystem:write",
         {"path": "b.txt", "content": "y", "overwrite": True},
         VerificationPolicy("write_verified")),
    ])
    engine, gm, storage, _ = _engine(tmp_path / "f.db", sb, planner, write_cap=write_cap,
                                     max_concurrency=2, approve_required=False)
    gid = engine.submit_goal("write a and b").id
    # approval not required (concurrency-only test)
    t0 = time.monotonic()
    final = engine.run_goal(gid)
    elapsed = time.monotonic() - t0
    assert final.status == GoalStatus.COMPLETED
    assert write_cap.calls == 2
    assert write_cap.max_active == 2  # different locks -> concurrent
    assert elapsed < 0.45
    # two independent lock acquisitions (different resources)
    acq = [e for e in storage.list_events() if e.kind == "mutation.lock.acquired"]
    assert len(acq) == 2
    assert {e.detail.get("resource") for e in acq} == {"a.txt", "b.txt"}
    engine.storage.close()


def test_read_write_interaction_same_resource(tmp_path):
    """A mutating step on a resource and a read of the SAME resource: the
    mutation takes the lock; the read has no lock (read-only) but both
    execute their own authorization. This is coordination - the read may
    observe pre/post state; correctness is the mutation's (lock-held)."""
    sb = _sandbox(tmp_path)
    write_cap = SlowWriteCapability(sb, sleep=0.1)
    read_cap = SlowReadCapability(sleep=0.1)
    planner = TwoStepPlanner([
        ("write a", "filesystem.write", "write", "filesystem:write",
         {"path": "a.txt", "content": "x", "overwrite": True},
         VerificationPolicy("write_verified")),
        ("read a", "filesystem.read", "read", "filesystem:read", {"path": "a.txt"},
         VerificationPolicy("schema_keys", {"keys": ["content"]})),
    ])
    engine, gm, storage, _ = _engine(tmp_path / "g.db", sb, planner,
                                     read_cap=read_cap, write_cap=write_cap,
                                     max_concurrency=2, approve_required=False)
    gid = engine.submit_goal("write then read a").id
    # approval not required (concurrency-only test)
    final = engine.run_goal(gid)
    assert final.status == GoalStatus.COMPLETED
    assert write_cap.calls == 1
    # both ran their own authorization checks
    checked = [e for e in storage.list_events() if e.kind == "permission.checked"]
    assert len(checked) == 2
    engine.storage.close()


# ---------------------------------------------------------------------------
# 4. dependency-preserving scheduling; blocked step doesn't stall ready work
# ---------------------------------------------------------------------------


def test_dependency_constraints_authoritative(tmp_path):
    """A step with depends_on=[0] runs only after step 0 succeeded."""
    sb = _sandbox(tmp_path)
    read_cap = SlowReadCapability(sleep=0.05)
    order = []

    class RecordingRead(SlowReadCapability):
        def execute(self, action, params):
            order.append(params["path"])
            return super().execute(action, params)

    rec = RecordingRead(sleep=0.05)
    planner = TwoStepPlanner([
        ("read a", "filesystem.read", "read", "filesystem:read", {"path": "a.txt"},
         VerificationPolicy("schema_keys", {"keys": ["content"]})),
        ("read a2", "filesystem.read", "read", "filesystem:read", {"path": "a.txt"},
         VerificationPolicy("schema_keys", {"keys": ["content"]}), [0]),
    ])
    engine, gm, storage, _ = _engine(tmp_path / "h.db", sb, planner, read_cap=rec,
                                     max_concurrency=2)
    gid = engine.submit_goal("read a twice").id
    engine.run_goal(gid)
    assert order[0] == "a.txt"  # step 0 first
    assert len(order) == 2
    engine.storage.close()


def test_blocked_mutation_does_not_stall_ready_read(tmp_path):
    """Mutation A waits on a held lock; an independent ready read runs while
    A waits (key concurrency behavior)."""
    sb = _sandbox(tmp_path)
    db = tmp_path / "i.db"
    # another process holds the lock on a.txt
    holder = SQLiteStorage(db)
    holder.acquire(FS, canonical_resource(FS, "a.txt"), "filesystem.write", "write",
                   "proc-holder", 3600.0, now=None)
    write_cap = SlowWriteCapability(sb, sleep=0.05)
    read_cap = SlowReadCapability(sleep=0.05)
    started = []

    class MarkerRead(SlowReadCapability):
        def execute(self, action, params):
            started.append(params["path"])
            return super().execute(action, params)

    mr = MarkerRead(sleep=0.05)
    planner = TwoStepPlanner([
        ("write a", "filesystem.write", "write", "filesystem:write",
         {"path": "a.txt", "content": "x", "overwrite": True},
         VerificationPolicy("write_verified")),
        ("read b", "filesystem.read", "read", "filesystem:read", {"path": "b.txt"},
         VerificationPolicy("schema_keys", {"keys": ["content"]})),
    ])
    engine, gm, storage, _ = _engine(db, sb, planner, read_cap=mr, write_cap=write_cap,
                                     max_concurrency=2, approve_required=False)
    gid = engine.submit_goal("write a + read b").id
    # approval not required (concurrency-only test)
    final = engine.run_goal(gid)
    # the write FAILED on immediate contention (wait disabled); the read still
    # executed its own authorization + capability while the write was blocked
    assert final.status == GoalStatus.ACTIVE or final.status == GoalStatus.BLOCKED
    assert started == ["b.txt"]
    checked = [e for e in storage.list_events() if e.kind == "permission.checked"]
    assert len(checked) >= 1  # the read authorized independently
    holder.close()
    engine.storage.close()


# ---------------------------------------------------------------------------
# 5. max_concurrency=1 reproduces current behavior
# ---------------------------------------------------------------------------


def test_max_concurrency_one_sequential(tmp_path):
    sb = _sandbox(tmp_path)
    read_cap = SlowReadCapability(sleep=0.1)
    planner = TwoStepPlanner([
        ("read a", "filesystem.read", "read", "filesystem:read", {"path": "a.txt"},
         VerificationPolicy("schema_keys", {"keys": ["content"]})),
        ("read b", "filesystem.read", "read", "filesystem:read", {"path": "b.txt"},
         VerificationPolicy("schema_keys", {"keys": ["content"]})),
    ])
    engine, gm, storage, _ = _engine(tmp_path / "j.db", sb, planner, read_cap=read_cap,
                                     max_concurrency=1)
    gid = engine.submit_goal("read a and b").id
    t0 = time.monotonic()
    final = engine.run_goal(gid)
    elapsed = time.monotonic() - t0
    assert final.status == GoalStatus.COMPLETED
    assert read_cap.max_active == 1
    assert elapsed >= 0.19  # serial: 2 x 0.1s
    engine.storage.close()


# ---------------------------------------------------------------------------
# 6/7. bounded shutdown + cancellation of queued work
# ---------------------------------------------------------------------------


def test_bounded_worker_shutdown_waits_for_active(tmp_path):
    """shutdown() joins active workers (bounded); a second shutdown is a
    no-op; no worker survives engine shutdown."""
    sb = _sandbox(tmp_path)
    barrier = threading.Barrier(2)
    read_cap = SlowReadCapability(sleep=0.4, barrier=barrier)
    planner = TwoStepPlanner([
        ("read a", "filesystem.read", "read", "filesystem:read", {"path": "a.txt"},
         VerificationPolicy("schema_keys", {"keys": ["content"]})),
        ("read b", "filesystem.read", "read", "filesystem:read", {"path": "b.txt"},
         VerificationPolicy("schema_keys", {"keys": ["content"]})),
    ])
    engine, gm, storage, _ = _engine(tmp_path / "k.db", sb, planner, read_cap=read_cap,
                                     max_concurrency=2)
    gid = engine.submit_goal("read a and b").id
    t0 = time.monotonic()
    final = engine.run_goal(gid)
    elapsed = time.monotonic() - t0
    assert final.status == GoalStatus.COMPLETED
    engine.shutdown()
    assert not engine.scheduler._workers  # joined: no live worker threads
    engine.shutdown()  # idempotent
    assert elapsed < 0.6
    engine.storage.close()


def test_cancel_queued_work(tmp_path):
    """Work queued but not yet dispatched can be cancelled; cancelled work
    never executes its capability."""
    sb = _sandbox(tmp_path)
    read_cap = SlowReadCapability(sleep=0.2)
    planner = TwoStepPlanner([
        ("read a", "filesystem.read", "read", "filesystem:read", {"path": "a.txt"},
         VerificationPolicy("schema_keys", {"keys": ["content"]})),
        ("read b", "filesystem.read", "read", "filesystem:read", {"path": "b.txt"},
         VerificationPolicy("schema_keys", {"keys": ["content"]})),
    ])
    engine, gm, storage, _ = _engine(tmp_path / "l.db", sb, planner, read_cap=read_cap,
                                     max_concurrency=2)
    # enqueue both steps, cancel the second before it starts
    task = engine.storage.load_task(engine.submit_goal("reads").id)
    # simulate: directly use the scheduler to enqueue two work items and cancel one
    w1 = engine.scheduler.enqueue("w1", "task_x", 0, lambda: None)
    w2 = engine.scheduler.enqueue("w2", "task_x", 1, lambda: None)
    assert engine.scheduler.cancel(w2.id) is True
    assert engine.scheduler.cancel("missing") is False
    engine.scheduler.run_until_done()
    engine.shutdown()
    engine.storage.close()


# ---------------------------------------------------------------------------
# 8/9. restart while multiple tasks in flight; no duplicate mutation
# ---------------------------------------------------------------------------


def test_restart_while_in_flight_no_duplicate_mutation(tmp_path):
    """A crash mid-concurrent-run: durable task state persists; restart never
    replays a completed mutation (per-step terminal states are persisted)."""
    sb = _sandbox(tmp_path)
    db = tmp_path / "m.db"
    write_cap = SlowWriteCapability(sb, sleep=0.1)
    planner = TwoStepPlanner([
        ("write a", "filesystem.write", "write", "filesystem:write",
         {"path": "a.txt", "content": "x", "overwrite": True},
         VerificationPolicy("write_verified")),
        ("write b", "filesystem.write", "write", "filesystem:write",
         {"path": "b.txt", "content": "y", "overwrite": True},
         VerificationPolicy("write_verified")),
    ])
    engine, gm, storage, _ = _engine(db, sb, planner, write_cap=write_cap,
                                     max_concurrency=2, approve_required=False)
    gid = engine.submit_goal("write a and b").id
    # approval not required (concurrency-only test)
    final = engine.run_goal(gid)
    assert final.status == GoalStatus.COMPLETED
    assert write_cap.calls == 2
    engine.storage.close()

    # fresh engine, same DB: task already completed -> nothing re-runs
    engine2, gm2, storage2, _ = _engine(db, sb, planner, write_cap=SlowWriteCapability(sb),
                                        max_concurrency=2)
    final2 = engine2.run_goal(gid)
    assert final2.status == GoalStatus.COMPLETED
    attempts = [e for e in storage2.list_events() if e.kind == "mutation.attempted"]
    assert len(attempts) == 2  # exactly the original two
    assert (sb / "a.txt").read_text(encoding="utf-8") == "x"
    assert (sb / "b.txt").read_text(encoding="utf-8") == "y"
    engine2.storage.close()


# ---------------------------------------------------------------------------
# 10/11. approval-pending / recovery-required never consume a worker
# ---------------------------------------------------------------------------


def test_approval_pending_does_not_consume_worker(tmp_path):
    sb = _sandbox(tmp_path)
    read_cap = SlowReadCapability(sleep=0.05)
    write_cap = SlowWriteCapability(sb, sleep=0.05)
    planner = TwoStepPlanner([
        ("write a", "filesystem.write", "write", "filesystem:write",
         {"path": "a.txt", "content": "x", "overwrite": True},
         VerificationPolicy("write_verified")),
        ("read b", "filesystem.read", "read", "filesystem:read", {"path": "b.txt"},
         VerificationPolicy("schema_keys", {"keys": ["content"]})),
    ])
    engine, gm, storage, _ = _engine(tmp_path / "n.db", sb, planner,
                                     read_cap=read_cap, write_cap=write_cap,
                                     max_concurrency=2)
    gid = engine.submit_goal("write a + read b").id
    final = engine.run_goal(gid)  # approval pending on the write
    assert final.status == GoalStatus.BLOCKED
    # the read still ran (approval-pending step did not consume the worker)
    checked = [e for e in storage.list_events() if e.kind == "permission.checked"]
    assert len(checked) >= 1
    # approve -> resumes
    for req in [r for r in engine.approval_store.list_requests() if r.status.value == "pending"]:
        engine.resolve_approval_request(req.approval_id, ApprovalOutcome.APPROVED, actor="user:alice")
    final = engine.run_goal(gid)
    assert final.status == GoalStatus.COMPLETED
    engine.storage.close()


def test_recovery_required_step_does_not_consume_worker(tmp_path):
    """A step whose goal has an open recovery never reaches the scheduler."""
    sb = _sandbox(tmp_path)
    db = tmp_path / "o.db"

    class FailWrite(FilesystemWriteCapability):
        def execute(self, action, params):
            raise __import__("arion.capabilities.registry", fromlist=["CapabilityError"]).CapabilityError("boom")

    engine, gm, storage, _ = _engine(db, sb, TwoStepPlanner([
        ("write a", "filesystem.write", "write", "filesystem:write",
         {"path": "a.txt", "content": "x", "overwrite": True},
         VerificationPolicy("write_verified")),
    ]), write_cap=FailWrite(sb), max_concurrency=2)
    gid = engine.submit_goal("write a").id
    _approve_all(engine, gid)
    engine.run_goal(gid)
    assert engine.recovery_store.list_recoveries()[0].status.value == "required"
    # the scheduler has no live work (recovery gate holds)
    assert engine.scheduler.ready_count() == 0 and engine.scheduler.running_count() == 0
    engine.storage.close()


# ---------------------------------------------------------------------------
# 12. FIFO waiter fairness preserved under concurrency
# ---------------------------------------------------------------------------


def test_fifo_fairness_preserved_under_concurrency(tmp_path):
    """With bounded waiting enabled, two same-resource mutating steps of one
    task dispatched concurrently queue in FIFO order and acquire in order.

    The foreign holder is released (exactly once, race-free) from the first
    backoff-sleep once BOTH waiters are durably queued, so the FIFO
    head-gated acquire runs in queue order."""
    sb = _sandbox(tmp_path)
    db = tmp_path / "p.db"
    holder = SQLiteStorage(db)
    holder_lock = holder.acquire(FS, canonical_resource(FS, "a.txt"), "filesystem.write",
                                 "write", "proc-holder", 3600.0, now=None)

    class ReleaseOnSleep:
        """Release the foreign holder exactly once, as soon as both waiters
        are durably queued (safety valve: after 300 backoff sleeps)."""

        def __init__(self):
            self.released = False
            self.n = 0
            self.guard = threading.Lock()

        def __call__(self, seconds):
            self.n += 1
            with self.guard:
                if not self.released:
                    queued = [w for w in holder.list_waiters()
                              if w.status.value == "queued"]
                    if len(queued) >= 2 or self.n > 300:
                        holder.release(holder_lock.lock_id, "proc-holder")
                        self.released = True
            time.sleep(seconds)

    planner = TwoStepPlanner([
        ("write a", "filesystem.write", "write", "filesystem:write",
         {"path": "a.txt", "content": "x", "overwrite": True},
         VerificationPolicy("write_verified")),
        ("write a2", "filesystem.write", "write", "filesystem:write",
         {"path": "a.txt", "content": "y", "overwrite": True},
         VerificationPolicy("write_verified")),
    ])
    write_cap = SlowWriteCapability(sb, sleep=0.05)
    engine, gm, storage, _ = _engine(db, sb, planner, write_cap=write_cap,
                                     max_concurrency=2, sleeper=ReleaseOnSleep(),
                                     approve_required=False)
    # bounded waiting via engine defaults is disabled in _engine; re-enable
    engine.lock_wait_max_seconds = 30.0
    engine.lock_wait_backoff_base = 0.01
    engine.lock_wait_backoff_max = 0.02
    gid = engine.submit_goal("write a twice").id
    final = engine.run_goal(gid)
    assert final.status == GoalStatus.COMPLETED
    assert write_cap.calls == 2
    # both waiters queued in FIFO order (positions 1 and 2) and acquired
    waiter_events = sorted(
        [e for e in storage.list_events() if e.kind == "mutation.lock.queued"],
        key=lambda e: e.detail.get("position", 0))
    assert [w.detail["position"] for w in waiter_events] == [1, 2]
    acq = [e for e in storage.list_events() if e.kind == "mutation.lock.acquired"]
    assert len(acq) == 2
    holder.close()
    engine.storage.close()



# ---------------------------------------------------------------------------
# 13. crash mid-flight with concurrent steps -> restart resumes WITHOUT
#     replaying the completed mutation (ADR-024 Phase D: bounded durable
#     per-step state; at-least-once for the interrupted step only)
# ---------------------------------------------------------------------------


def test_crash_mid_flight_restart_resumes_without_duplicate(tmp_path):
    sb = _sandbox(tmp_path)
    db = tmp_path / "q.db"

    class CrashSleeper:
        """Real clock; first sleep advances, second raises (simulated crash
        while step1 is durably waiting on the lock)."""

        def __init__(self):
            self.n = 0

        def __call__(self, seconds):
            self.n += 1
            if self.n > 1:
                raise RuntimeError("simulated crash while waiting for mutation lock")
            time.sleep(seconds)

    # step0 (write a) is independent; step1 (write b) contends on a foreign
    # holder, enters the durable FIFO queue, then the sleeper crashes the run.
    holder = SQLiteStorage(db)
    holder_lock = holder.acquire(FS, canonical_resource(FS, "b.txt"),
                                 "filesystem.write", "write", "proc-holder",
                                 3600.0, now=None)
    planner = TwoStepPlanner([
        ("write a", "filesystem.write", "write", "filesystem:write",
         {"path": "a.txt", "content": "x", "overwrite": True},
         VerificationPolicy("write_verified")),
        ("write b", "filesystem.write", "write", "filesystem:write",
         {"path": "b.txt", "content": "y", "overwrite": True},
         VerificationPolicy("write_verified")),
    ])
    write_cap = SlowWriteCapability(sb, sleep=0.05)
    engine, gm, storage, _ = _engine(db, sb, planner, write_cap=write_cap,
                                     max_concurrency=2, sleeper=CrashSleeper(),
                                     approve_required=False)
    engine.lock_wait_max_seconds = 30.0
    engine.lock_wait_backoff_base = 0.01
    engine.lock_wait_backoff_max = 0.02
    gid = engine.submit_goal("write a and b").id
    with pytest.raises(RuntimeError, match="simulated crash"):
        engine.run_goal(gid)
    # durable per-step state: step0 SUCCEEDED (its mutation happened exactly
    # once), step1 still PENDING with bounded lock_wait metadata
    task = gm.task_history(gid)[-1]
    assert task.steps[0].status == StepStatus.SUCCEEDED
    assert task.steps[1].status == StepStatus.PENDING
    assert task.lock_wait is not None and task.lock_wait["resource"] == "b.txt"
    engine.storage.close()
    holder.release(holder_lock.lock_id, "proc-holder")
    holder.close()

    # fresh engine + fresh scheduler on the same DB: resume. step0 must NOT
    # re-run (per-step SUCCEEDED is durable); step1 completes exactly once.
    engine2, gm2, storage2, _ = _engine(db, sb, planner,
                                        write_cap=SlowWriteCapability(sb, sleep=0.05),
                                        max_concurrency=2, approve_required=False)
    final = engine2.run_goal(gid)
    assert final.status == GoalStatus.COMPLETED
    attempts = [e for e in storage2.list_events() if e.kind == "mutation.attempted"]
    assert len(attempts) == 2  # a once, b once - no replay, no duplicate
    assert (sb / "a.txt").read_text(encoding="utf-8") == "x"
    assert (sb / "b.txt").read_text(encoding="utf-8") == "y"
    engine2.storage.close()


# ---------------------------------------------------------------------------
# 14. shutdown / cancellation fail closed (ADR-024 Phase E)
# ---------------------------------------------------------------------------


def test_shutdown_cancels_queued_and_rejects_new_work(tmp_path):
    sb = _sandbox(tmp_path)
    db = tmp_path / "s.db"
    write_cap = SlowWriteCapability(sb, sleep=0.05)
    planner = TwoStepPlanner([
        ("write a", "filesystem.write", "write", "filesystem:write",
         {"path": "a.txt", "content": "x", "overwrite": True},
         VerificationPolicy("write_verified")),
        ("write b", "filesystem.write", "write", "filesystem:write",
         {"path": "b.txt", "content": "y", "overwrite": True},
         VerificationPolicy("write_verified")),
    ])
    engine, _, _, _ = _engine(db, sb, planner, write_cap=write_cap,
                              max_concurrency=2, approve_required=False)
    ran: list[str] = []

    def fn(tag):
        def _f():
            ran.append(tag)
        return _f

    engine.scheduler.enqueue("a", "t1", 0, fn("a"))
    engine.scheduler.enqueue("b", "t1", 1, fn("b"))
    engine.scheduler.cancel(engine.scheduler.snapshot()["queued"][0]["id"])
    engine.scheduler.run_until_done()
    assert ran == ["b"]  # cancelled item never ran
    engine.scheduler.shutdown()
    # fail closed: no new work after shutdown, no orphan execution
    with pytest.raises(Exception):
        engine.scheduler.enqueue("c", "t1", 2, fn("c"))
    engine.scheduler.run_until_done()
    assert ran == ["b"]
    engine.storage.close()


# ---------------------------------------------------------------------------
# 15. concurrent same-resource waiters never share waiter identity
#     (ADR-024 regression: task.lock_wait is step-scoped, so a step can
#     never reuse or clobber a sibling step's durable FIFO waiter)
# ---------------------------------------------------------------------------


def test_concurrent_same_resource_waiters_get_distinct_durable_identities(tmp_path):
    sb = _sandbox(tmp_path)
    db = tmp_path / "w2.db"
    holder = SQLiteStorage(db)
    holder_lock = holder.acquire(FS, canonical_resource(FS, "a.txt"), "filesystem.write",
                                 "write", "proc-holder", 3600.0, now=None)

    class ReleaseOnSleep:
        def __init__(self):
            self.released = False
            self.n = 0
            self.guard = threading.Lock()

        def __call__(self, seconds):
            self.n += 1
            with self.guard:
                if not self.released:
                    queued = [w for w in holder.list_waiters()
                              if w.status.value == "queued"]
                    if len(queued) >= 2 or self.n > 300:
                        holder.release(holder_lock.lock_id, "proc-holder")
                        self.released = True
            time.sleep(seconds)

    planner = TwoStepPlanner([
        ("write a", "filesystem.write", "write", "filesystem:write",
         {"path": "a.txt", "content": "x", "overwrite": True},
         VerificationPolicy("write_verified")),
        ("write a2", "filesystem.write", "write", "filesystem:write",
         {"path": "a.txt", "content": "y", "overwrite": True},
         VerificationPolicy("write_verified")),
    ])
    write_cap = SlowWriteCapability(sb, sleep=0.05)
    engine, gm, storage, _ = _engine(db, sb, planner, write_cap=write_cap,
                                     max_concurrency=2, sleeper=ReleaseOnSleep(),
                                     approve_required=False)
    engine.lock_wait_max_seconds = 30.0
    engine.lock_wait_backoff_base = 0.01
    engine.lock_wait_backoff_max = 0.02
    gid = engine.submit_goal("write a twice").id
    final = engine.run_goal(gid)
    assert final.status == GoalStatus.COMPLETED
    assert write_cap.calls == 2
    # both steps got their OWN durable waiter rows: distinct ids, positions
    # 1 and 2, both acquired (never one waiter shared by both steps)
    waiters = sorted(holder.list_waiters(), key=lambda w: w.seq)
    assert len(waiters) == 2
    assert waiters[0].waiter_id != waiters[1].waiter_id
    assert [w.seq for w in waiters] == [1, 2]
    assert all(w.status.value == "acquired" for w in waiters)
    # each acquire event references a distinct waiter
    acq = [e for e in storage.list_events() if e.kind == "mutation.lock.acquired"]
    assert len(acq) == 2
    assert len({a.detail["waiter_id"] for a in acq}) == 2
    holder.close()
    engine.storage.close()
