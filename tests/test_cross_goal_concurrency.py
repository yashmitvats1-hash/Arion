"""Cross-goal / multi-task shared scheduler (ADR-025, Phases B/C/D).

The shared scheduler lets multiple goals/tasks run through ONE bounded
scheduler instance with a global max_concurrency:

- independent tasks execute concurrently; total running workers never exceed
  the global bound; one goal cannot consume unlimited capacity (fair
  round-robin admission, per-goal per-round cap);
- a blocked / approval-pending / recovery-gated task consumes no worker;
- scheduler state is never authorization state;
- dependencies stay authoritative PER TASK (a step of goal A can never
  satisfy or bypass a dependency of goal B);
- mutation-lock integration is unchanged: live authorization -> approval ->
  durable lock -> FIFO queue; a mutating step whose resource is actively
  locked by another task is PARKED (durable waiter registered, no worker
  consumed) instead of occupying a worker.
"""

from __future__ import annotations

import threading
import time

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
    PendingApprovalHandler,
    RelativePathBoundary,
    ResourcePolicy,
)
from arion.orchestration.engine import ArionEngine
from arion.state.locks import LockWaiterStatus, canonical_resource
from arion.state.models import GoalStatus, PlanStep, StepStatus, TaskStatus, VerificationPolicy
from arion.state.scheduler_work import SchedulerWorkStatus
from arion.state.store import SQLiteStorage

FS = "filesystem:path"


class SlowReadCapability:
    """Read-only capability with sleep + concurrency tracking."""

    name = "filesystem.read"
    description = "slow read"
    actions = [AS(name="read", description="read", required_scope="filesystem:read",
                  risk="low", side_effects="read_only", reversible=True,
                  idempotent=True, retry_safe=True,
                  resource_kind=FS, resource_param="path",
                  param_schema={"path": {"type": "string", "required": True}},
                  default_verification={"policy": "schema_keys", "args": {"keys": ["content"]}})]

    def __init__(self, sleep=0.2, barrier=None):
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
    """Write capability with optional sleep; tracks overlap + call order."""

    def __init__(self, sandbox, sleep=0.0, barrier=None):
        super().__init__(sandbox)
        self.sleep = sleep
        self.barrier = barrier
        self.active = 0
        self.max_active = 0
        self.mlock = threading.Lock()
        self.calls = []

    def execute(self, action, params):
        with self.mlock:
            self.active += 1
            self.max_active = max(self.max_active, self.active)
            self.calls.append(params.get("path"))
        try:
            if self.barrier is not None:
                self.barrier.wait(timeout=5)
            time.sleep(self.sleep)
            return super().execute(action, dict(params))
        finally:
            with self.mlock:
                self.active -= 1


class TwoStepPlanner:
    """Hand-built plans (steps differ per task via a factory on the goal text)."""

    def __init__(self, steps_factory):
        self._factory = steps_factory

    def plan(self, goal_description, task_id, registry, context=None):
        steps = self._factory(goal_description)
        return [PlanStep(index=i, intent=s[0], capability=s[1], action=s[2],
                         scope=s[3], params=dict(s[4]), verification=s[5],
                         depends_on=list(s[6]) if len(s) > 6 else [])
                for i, s in enumerate(steps)]

    def required_capabilities(self, goal_description):
        caps = {s[1] for s in self._factory(goal_description)}
        return caps


def _read_step(path, depends_on=()):
    return (f"read {path}", "filesystem.read", "read", "filesystem:read", {"path": path},
            VerificationPolicy("schema_keys", {"keys": ["content"]}), list(depends_on))


def _write_step(path, content="x", depends_on=()):
    return (f"write {path}", "filesystem.write", "write", "filesystem:write",
            {"path": path, "content": content, "overwrite": True},
            VerificationPolicy("write_verified"), list(depends_on))


def _policy(approve_risk_high=True):
    return ResourcePolicy(
        allowed_scopes={"filesystem:read", "filesystem:write"},
        risk_deny=set(),
        risk_approve={"high"} if approve_risk_high else set(),
        boundaries={FS: RelativePathBoundary()},
    )


def _sandbox(tmp_path):
    sb = tmp_path / "wsandbox"
    sb.mkdir(parents=True, exist_ok=True)
    (sb / "a.txt").write_text("a", encoding="utf-8")
    (sb / "b.txt").write_text("b", encoding="utf-8")
    return sb


class _Env:
    def __init__(self, engine, gm, storage, registry, caps, sandbox):
        self.engine = engine
        self.gm = gm
        self.storage = storage
        self.registry = registry
        self.caps = caps
        self.sandbox = sandbox


def _env(tmp_path, planner, max_concurrency=2, lock_wait_max_seconds=0.0,
         read_cap=None, write_cap=None, sleeper=None, approve_risk_high=True,
         db_name="x.db"):
    sb = _sandbox(tmp_path)
    db = tmp_path / db_name
    storage = SQLiteStorage(db)
    registry = CapabilityRegistry()
    read_cap = read_cap or FilesystemReadCapability(sb)
    write_cap = write_cap or FilesystemWriteCapability(sb)
    registry.register(read_cap)
    registry.register(write_cap)
    events = EventLogger(sinks=[storage])
    cognitive = SQLiteCognitiveStore(db)
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
        policy=_policy(approve_risk_high=approve_risk_high),
        approval_handler=PendingApprovalHandler(),
        goal_manager=gm, world_monitor=wm,
        max_concurrency=max_concurrency,
        lock_wait_max_seconds=lock_wait_max_seconds,
        lock_clock=None, lock_sleeper=sleeper,
    )
    return _Env(engine, gm, storage, registry,
                {"read": read_cap, "write": write_cap}, sb)


def _submit(env, description):
    gid = env.engine.submit_goal(description).id
    env.engine._plan_for_goal(gid)
    return gid


def _task_for(env, goal_id):
    return next(t for t in env.storage.list_tasks() if t.goal_id == goal_id)


# --------------------------------------------------------------------------- #
# Phase B - shared scheduler: concurrency + global bound
# --------------------------------------------------------------------------- #


def test_two_goals_execute_concurrently(tmp_path):
    barrier = threading.Barrier(2)
    read_cap = SlowReadCapability(sleep=0.2, barrier=barrier)
    env = _env(tmp_path, TwoStepPlanner(lambda d: [_read_step("a.txt")]),
               max_concurrency=2, read_cap=read_cap)
    g1 = _submit(env, "goal one")
    g2 = _submit(env, "goal two")
    t0 = time.monotonic()
    results = env.engine.run_goals([g1, g2])
    elapsed = time.monotonic() - t0
    assert results[g1].status == GoalStatus.COMPLETED
    assert results[g2].status == GoalStatus.COMPLETED
    assert read_cap.max_active >= 2  # both read steps overlapped
    assert elapsed < 0.45, f"elapsed {elapsed} not materially below serial"
    env.engine.storage.close()


def test_global_max_concurrency_never_exceeded(tmp_path):
    """3 goals x 2 reads with max_concurrency=2: at most 2 in execute() ever."""
    read_cap = SlowReadCapability(sleep=0.15)
    env = _env(tmp_path, TwoStepPlanner(lambda d: [_read_step("a.txt"), _read_step("b.txt")]),
               max_concurrency=2, read_cap=read_cap)
    gids = [_submit(env, f"goal {i}") for i in range(3)]
    results = env.engine.run_goals(gids)
    assert all(results[g].status == GoalStatus.COMPLETED for g in gids)
    assert read_cap.max_active <= 2
    env.engine.storage.close()


def test_goal_cannot_consume_unlimited_capacity(tmp_path):
    """Goal A has 6 runnable reads, goal B has 1; with fair round-robin
    admission, B's step starts within the first two starts (no starvation)."""
    read_cap = SlowReadCapability(sleep=0.05)
    env = _env(tmp_path, TwoStepPlanner(
        lambda d: ([_read_step(f"f{i}.txt") for i in range(6)]
                   if "many" in d else [_read_step("b.txt")])),
        max_concurrency=2, read_cap=read_cap)
    g_many = _submit(env, "many steps goal")
    g_one = _submit(env, "one step goal")
    results = env.engine.run_goals([g_many, g_one])
    assert results[g_many].status == GoalStatus.COMPLETED
    assert results[g_one].status == GoalStatus.COMPLETED
    assert "b.txt" in read_cap.started[:2], read_cap.started
    env.engine.storage.close()


def test_approval_pending_goal_consumes_no_worker(tmp_path):
    """Goal A waits on approval (never occupies a worker while blocked);
    goal B's reads run to completion."""
    env = _env(tmp_path, TwoStepPlanner(
        lambda d: ([_write_step("a.txt")] if "write" in d
                   else [_read_step("a.txt"), _read_step("b.txt")])),
        max_concurrency=2, approve_risk_high=True)
    g_write = _submit(env, "write a")
    g_read = _submit(env, "read files")
    results = env.engine.run_goals([g_write, g_read])
    assert results[g_read].status == GoalStatus.COMPLETED
    write_task = _task_for(env, g_write)
    assert write_task.status == TaskStatus.AWAITING_APPROVAL
    # no worker is held by the blocked goal: no RUNNING registry row remains
    rows = env.engine.scheduler_registry.list_work(goal_id=g_write)
    assert all(r.status != SchedulerWorkStatus.RUNNING for r in rows)
    assert env.engine.scheduler.running_count() == 0
    env.engine.storage.close()


def test_recovery_gated_goal_consumes_no_worker(tmp_path):
    """Goal A has an open recovery; goal B's reads complete while A's step
    never reaches a worker."""

    class FailWrite(FilesystemWriteCapability):
        def execute(self, action, params):
            from arion.capabilities.registry import CapabilityError
            raise CapabilityError("boom")

    env = _env(tmp_path, TwoStepPlanner(
        lambda d: ([_write_step("a.txt")] if "write" in d
                   else [_read_step("a.txt"), _read_step("b.txt")])),
        max_concurrency=2, write_cap=FailWrite(_sandbox(tmp_path)),
        approve_risk_high=False)
    g_write = _submit(env, "write a")
    env.engine.run_goal(g_write)
    assert env.engine.recovery_store.list_recoveries()[0].status.value == "required"
    g_read = _submit(env, "read files")
    results = env.engine.run_goals([g_write, g_read])
    assert results[g_read].status == GoalStatus.COMPLETED
    rows = env.engine.scheduler_registry.list_work(goal_id=g_write)
    assert rows == [] or all(r.status != SchedulerWorkStatus.RUNNING for r in rows)
    env.engine.storage.close()


def test_single_task_through_shared_scheduler_matches_adr024(tmp_path):
    """run_tasks with one task and max_concurrency=1 reproduces the serial
    behavior; max_concurrency=2 gives concurrency (ADR-024 semantics intact)."""
    planner = TwoStepPlanner(lambda d: [_read_step("a.txt"), _read_step("b.txt")])

    env1 = _env(tmp_path, planner, max_concurrency=1, db_name="x1.db")
    g1 = _submit(env1, "serial")
    task1 = _task_for(env1, g1)
    env1.engine.run_tasks([task1.id])
    assert env1.storage.load_task(task1.id).status == TaskStatus.COMPLETED
    env1.engine.storage.close()

    read_cap = SlowReadCapability(sleep=0.15)
    env2 = _env(tmp_path, TwoStepPlanner(lambda d: [_read_step("a.txt"), _read_step("b.txt")]),
                max_concurrency=2, read_cap=read_cap, db_name="x2.db")
    g2 = _submit(env2, "parallel")
    task2 = _task_for(env2, g2)
    env2.engine.run_tasks([task2.id])
    assert env2.storage.load_task(task2.id).status == TaskStatus.COMPLETED
    assert read_cap.max_active >= 2
    env2.engine.storage.close()


def test_worker_count_is_bounded_under_shared_scheduler(tmp_path):
    """The shared scheduler never spawns more worker threads than
    max_concurrency regardless of how many tasks are active."""
    read_cap = SlowReadCapability(sleep=0.1)
    env = _env(tmp_path, TwoStepPlanner(lambda d: [_read_step("a.txt")]),
               max_concurrency=2, read_cap=read_cap)
    gids = [_submit(env, f"goal {i}") for i in range(4)]
    env.engine.run_goals(gids)
    assert env.engine.scheduler.snapshot()["workers"] <= 2
    env.engine.storage.close()


# --------------------------------------------------------------------------- #
# Phase C - cross-goal dependency isolation
# --------------------------------------------------------------------------- #


def test_same_step_index_across_tasks_dependencies_authoritative(tmp_path):
    """Both tasks have identical step indices (0 -> 1). A's step 0 completing
    must NEVER satisfy B's step-1 dependency: each b.txt step waits for its
    own task's a.txt step."""
    started = []
    lock = threading.Lock()

    class TrackedRead(FilesystemReadCapability):
        def execute(self, action, params):
            with lock:
                started.append(params.get("path"))
            time.sleep(0.15)
            return super().execute(action, params)

    env = _env(tmp_path, TwoStepPlanner(
        lambda d: [_read_step("a.txt"), _read_step("b.txt", depends_on=[0])]),
        max_concurrency=2, read_cap=TrackedRead(_sandbox(tmp_path)))
    g1 = _submit(env, "task one")
    g2 = _submit(env, "task two")
    env.engine.run_goals([g1, g2])
    assert started.index("a.txt") < started.index("b.txt")
    env.engine.storage.close()


def test_one_goal_failing_does_not_stop_other(tmp_path):
    """Goal A's read of a missing file fails the task; goal B still runs."""

    class FailRead(FilesystemReadCapability):
        def execute(self, action, params):
            if params.get("path") == "missing.txt":
                raise FileNotFoundError(params.get("path"))
            return super().execute(action, params)

    env = _env(tmp_path, TwoStepPlanner(
        lambda d: ([_read_step("missing.txt")] if "fail" in d
                   else [_read_step("a.txt"), _read_step("b.txt")])),
        max_concurrency=2, read_cap=FailRead(_sandbox(tmp_path)))
    g_fail = _submit(env, "fail goal")
    g_ok = _submit(env, "ok goal")
    results = env.engine.run_goals([g_fail, g_ok])
    assert results[g_ok].status == GoalStatus.COMPLETED
    assert results[g_fail].status == GoalStatus.FAILED
    env.engine.storage.close()


# --------------------------------------------------------------------------- #
# Phase D - mutation-lock integration across goals
# --------------------------------------------------------------------------- #


def test_same_resource_mutations_across_goals_serialize_fifo(tmp_path):
    """Two goals write the same file: only one owns the durable lock at a
    time, exactly two successful mutations, no duplicates."""
    write_cap = SlowWriteCapability(_sandbox(tmp_path), sleep=0.15)
    env = _env(tmp_path, TwoStepPlanner(
        lambda d: [_write_step("a.txt", content=d)]),
        max_concurrency=2, write_cap=write_cap, lock_wait_max_seconds=10.0,
        approve_risk_high=False)
    g1 = _submit(env, "first")
    g2 = _submit(env, "second")
    results = env.engine.run_goals([g1, g2])
    assert results[g1].status == GoalStatus.COMPLETED
    assert results[g2].status == GoalStatus.COMPLETED
    assert write_cap.max_active == 1            # never concurrent on same file
    assert write_cap.calls.count("a.txt") == 2  # exactly two mutations
    content = (env.sandbox / "a.txt").read_text()
    assert content in ("first", "second")       # last writer won exactly once
    env.engine.storage.close()


def test_different_resource_mutations_across_goals_concurrent(tmp_path):
    """Two goals write DIFFERENT files: both hold their own lock concurrently."""
    write_cap = SlowWriteCapability(_sandbox(tmp_path), sleep=0.2)
    env = _env(tmp_path, TwoStepPlanner(
        lambda d: [_write_step("a.txt" if "one" in d else "b.txt")]),
        max_concurrency=2, write_cap=write_cap, lock_wait_max_seconds=0.0,
        approve_risk_high=False)
    g1 = _submit(env, "goal one")
    g2 = _submit(env, "goal two")
    t0 = time.monotonic()
    results = env.engine.run_goals([g1, g2])
    elapsed = time.monotonic() - t0
    assert results[g1].status == GoalStatus.COMPLETED
    assert results[g2].status == GoalStatus.COMPLETED
    assert write_cap.max_active >= 2
    assert elapsed < 0.45
    env.engine.storage.close()


def test_parked_waiter_consumes_no_worker(tmp_path):
    """Goal A holds the lock on a.txt (slow write). Goal B's write to a.txt is
    PARKED: it registers a durable waiter but consumes no worker and creates
    no RUNNING registry row; goal C's read runs while B waits."""
    write_cap = SlowWriteCapability(_sandbox(tmp_path), sleep=0.3)
    env = _env(tmp_path, TwoStepPlanner(
        lambda d: ([_write_step("a.txt")] if "w" in d else [_read_step("b.txt")])),
        max_concurrency=2, write_cap=write_cap, lock_wait_max_seconds=10.0,
        approve_risk_high=False)
    g_w = _submit(env, "w A")
    g_w2 = _submit(env, "w B")
    g_r = _submit(env, "read C")
    results = env.engine.run_goals([g_w, g_w2, g_r])
    assert results[g_w].status == GoalStatus.COMPLETED
    assert results[g_w2].status == GoalStatus.COMPLETED
    assert results[g_r].status == GoalStatus.COMPLETED
    # B was parked (no worker), so the write capability never ran twice at
    # once: exactly two serialized writes to a.txt, A strictly before B
    assert write_cap.max_active == 1
    assert write_cap.calls == ["a.txt", "a.txt"]
    # B went through the durable FIFO waiter queue
    b_task = _task_for(env, g_w2)
    waiters = [w for w in env.storage.list_waiters() if w.task_id == b_task.id]
    assert any(w.status == LockWaiterStatus.ACQUIRED for w in waiters)
    env.engine.storage.close()


def test_mutation_lock_acquired_only_after_live_authz_cross_goal(tmp_path):
    """Audit ordering per goal: permission.checked precedes
    mutation.lock.acquired for every cross-goal mutation."""
    write_cap = SlowWriteCapability(_sandbox(tmp_path), sleep=0.05)
    env = _env(tmp_path, TwoStepPlanner(
        lambda d: [_write_step("a.txt" if "one" in d else "b.txt")]),
        max_concurrency=2, write_cap=write_cap, lock_wait_max_seconds=0.0,
        approve_risk_high=False)
    g1 = _submit(env, "goal one")
    g2 = _submit(env, "goal two")
    env.engine.run_goals([g1, g2])
    events = env.storage.list_events()
    for goal_gid in (g1, g2):
        task = _task_for(env, goal_gid)
        acquired = [e for e in events if e.kind == "mutation.lock.acquired"
                    and e.task_id == task.id]
        assert acquired, f"no lock acquired for {goal_gid}"
        for a in acquired:
            prior = [e for e in events
                     if e.task_id == task.id and e.step_id == a.step_id
                     and e.kind == "permission.checked"]
            assert prior, f"no live authz before lock acquire for {goal_gid}"
    env.engine.storage.close()


def test_scheduler_availability_never_bypasses_lock_queue(tmp_path):
    """Even with free workers, a step waiting on a locked resource stays
    parked: worker availability is never a substitute for the durable FIFO."""
    write_cap = SlowWriteCapability(_sandbox(tmp_path), sleep=0.25)
    env = _env(tmp_path, TwoStepPlanner(
        lambda d: ([_write_step("a.txt")] if "w" in d else [_read_step("b.txt")])),
        max_concurrency=2, write_cap=write_cap, lock_wait_max_seconds=10.0,
        approve_risk_high=False)
    # foreign holder (another process) owns a.txt
    holder = env.storage.acquire(FS, canonical_resource(FS, "a.txt"),
                                 "filesystem.write", "write", "proc-foreign", 3600.0)
    g_w = _submit(env, "w A")
    g_r = _submit(env, "read C")
    results = env.engine.run_goals([g_w, g_r])
    assert results[g_r].status == GoalStatus.COMPLETED
    # write goal stopped cleanly while parked (durable waiter registered)
    write_task = _task_for(env, g_w)
    assert write_task.status == TaskStatus.RUNNING
    assert write_task.lock_wait is not None
    assert write_task.lock_wait.get("waiter_id")
    assert write_cap.calls == []
    # release the foreign lock -> parked step acquires via FIFO and completes
    env.storage.release(holder.lock_id, "proc-foreign")
    results = env.engine.run_goals([g_w, g_r])
    assert results[g_w].status == GoalStatus.COMPLETED
    assert write_cap.calls == ["a.txt"]
    env.engine.storage.close()


def test_registry_rows_reflect_shared_worker_lifecycle(tmp_path):
    """While two goals run concurrently, the durable registry shows terminal
    rows afterwards - one COMPLETED row per dispatched step, no RUNNING."""
    read_cap = SlowReadCapability(sleep=0.15)
    env = _env(tmp_path, TwoStepPlanner(lambda d: [_read_step("a.txt")]),
               max_concurrency=2, read_cap=read_cap)
    g1 = _submit(env, "goal one")
    g2 = _submit(env, "goal two")
    env.engine.run_goals([g1, g2])
    rows = env.engine.scheduler_registry.list_work()
    completed = [r for r in rows if r.status == SchedulerWorkStatus.COMPLETED]
    assert len(completed) == 2
    assert [r for r in rows if r.status == SchedulerWorkStatus.RUNNING] == []
    # bounded metadata: no engine objects in the rows
    for r in completed:
        d = r.to_dict()
        assert "fn" not in d and "thread" not in d and "content" not in d
    env.engine.storage.close()


def test_fair_round_robin_bounded_window(tmp_path):
    """Goal A submits many runnable steps; goal B submits one. B receives a
    worker within the first round (bounded scheduling window)."""
    read_cap = SlowReadCapability(sleep=0.03)
    env = _env(tmp_path, TwoStepPlanner(
        lambda d: ([_read_step(f"f{i}.txt") for i in range(8)]
                   if "many" in d else [_read_step("b.txt")])),
        max_concurrency=2, read_cap=read_cap)
    g_many = _submit(env, "many steps goal")
    g_one = _submit(env, "one step goal")
    env.engine.run_goals([g_many, g_one])
    assert read_cap.started.count("b.txt") == 1
    assert "b.txt" in read_cap.started[:2]
    env.engine.storage.close()
