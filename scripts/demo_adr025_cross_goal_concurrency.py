#!/usr/bin/env python3
"""ADR-025 DoD demo: cross-goal durable concurrency (shared scheduler).

Multiple goals/tasks share ONE bounded in-process scheduler with a global
max_concurrency and a DURABLE scheduler/work registry (SQLite). The
boundary from ADR-024 is unchanged and now spans goals:

  Scheduler coordinates execution.
  Durable lock store coordinates mutation ownership.
  Authorization is the only authority that permits execution.

  A  two independent goals execute concurrently (overlapping steps, wall
     clock materially below serial, one registry row per dispatch).
  B  two goals contend for the same mutation resource: the durable FIFO
     waiter queue stays authoritative (positions 1,2), writes serialize,
     both succeed exactly once - no duplicates.
  C  one goal waits for approval while another goal continues executing
     (the blocked goal consumes no worker).
  D  one goal enters recovery while another goal continues executing.
  E  scheduler restart with multiple goals preserves durable state and
     never replays a completed mutation (fresh engine = restart).
  F  fairness: a goal with many runnable steps cannot starve a goal with
     one step (round-robin admission, bounded window).

Deterministic and offline: no LLM, no network, no shell. The persistence
boundary is additionally proven across REAL processes by
tests/test_scheduler_restart.py (subprocess crash-running).
"""

from __future__ import annotations

import sys
import tempfile
import threading
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from arion.capabilities.filesystem import FilesystemReadCapability
from arion.capabilities.registry import CapabilityRegistry
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
from arion.state.locks import LockWaiterStatus
from arion.state.models import GoalStatus, PlanStep, StepStatus, TaskStatus, VerificationPolicy
from arion.state.scheduler_work import SchedulerWorkStatus
from arion.state.store import SQLiteStorage

FS = "filesystem:path"
CHECKS = 0


def check(cond: bool, msg: str) -> None:
    global CHECKS
    CHECKS += 1
    if not cond:
        print(f"  FAIL: {msg}")
        sys.exit(1)
    print(f"  ok: {msg}")


# --------------------------------------------------------------------------- #
# deterministic capabilities
# --------------------------------------------------------------------------- #


class SlowReadCapability(FilesystemReadCapability):
    def __init__(self, sandbox, sleep=0.2, barrier=None):
        super().__init__(sandbox)
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
            return super().execute(action, params)
        finally:
            with self.lock:
                self.active -= 1


class SlowWriteCapability(FilesystemWriteCapability):
    def __init__(self, sandbox, sleep=0.15):
        super().__init__(sandbox)
        self.sleep = sleep
        self.active = 0
        self.max_active = 0
        self.lock = threading.Lock()
        self.calls = []

    def execute(self, action, params):
        with self.lock:
            self.active += 1
            self.max_active = max(self.max_active, self.active)
            self.calls.append(params.get("path"))
        try:
            time.sleep(self.sleep)
            return super().execute(action, dict(params))
        finally:
            with self.lock:
                self.active -= 1


class FailWrite(FilesystemWriteCapability):
    def execute(self, action, params):
        from arion.capabilities.registry import CapabilityError
        raise CapabilityError("simulated disk failure")


class StepPlanner:
    def __init__(self, steps_factory):
        self._factory = steps_factory

    def plan(self, goal_description, task_id, registry, context=None):
        steps = self._factory(goal_description)
        return [PlanStep(index=i, intent=s[0], capability=s[1], action=s[2],
                         scope=s[3], params=dict(s[4]), verification=s[5],
                         depends_on=list(s[6]) if len(s) > 6 else [])
                for i, s in enumerate(steps)]

    def required_capabilities(self, goal_description):
        return {s[1] for s in self._factory(goal_description)}


def _read(path):
    return (f"read {path}", "filesystem.read", "read", "filesystem:read", {"path": path},
            VerificationPolicy("schema_keys", {"keys": ["content"]}), [])


def _write(path, content="x"):
    return (f"write {path}", "filesystem.write", "write", "filesystem:write",
            {"path": path, "content": content, "overwrite": True},
            VerificationPolicy("write_verified"), [])


def _engine(db, sb, planner, read_cap=None, write_cap=None, max_concurrency=2,
            lock_wait_max_seconds=0.0, approve_risk_high=False):
    storage = SQLiteStorage(db)
    registry = CapabilityRegistry()
    registry.register(read_cap or FilesystemReadCapability(sb))
    registry.register(write_cap or FilesystemWriteCapability(sb))
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
    policy = ResourcePolicy(
        allowed_scopes={"filesystem:read", "filesystem:write"},
        risk_deny=set(),
        risk_approve={"high"} if approve_risk_high else set(),
        boundaries={FS: RelativePathBoundary()},
    )
    engine = ArionEngine(
        storage=storage, registry=registry, planner=planner,
        router=DeterministicRouter(planner), events=events,
        policy=policy, approval_handler=PendingApprovalHandler(),
        goal_manager=gm, world_monitor=wm,
        max_concurrency=max_concurrency,
        lock_wait_max_seconds=lock_wait_max_seconds,
    )
    return engine, gm, storage


def _submit(engine, description):
    gid = engine.submit_goal(description).id
    engine._plan_for_goal(gid)
    return gid


def main() -> int:
    print("ADR-025 demo: cross-goal durable concurrency\n")
    tmp = Path(tempfile.mkdtemp(prefix="arion-adr025-"))
    sb = tmp / "wsandbox"
    sb.mkdir(parents=True, exist_ok=True)
    (sb / "a.txt").write_text("a", encoding="utf-8")
    (sb / "b.txt").write_text("b", encoding="utf-8")
    db = tmp / "adr025.db"

    # ---------------------------------------------------------------- A -----
    print("A. two independent goals execute concurrently")
    barrier = threading.Barrier(2)
    read_cap = SlowReadCapability(sb, sleep=0.2, barrier=barrier)
    engine, gm, storage = _engine(db, sb, StepPlanner(lambda d: [_read("a.txt")]),
                                  read_cap=read_cap, max_concurrency=2)
    g1 = _submit(engine, "goal one")
    g2 = _submit(engine, "goal two")
    t0 = time.monotonic()
    results = engine.run_goals([g1, g2])
    elapsed = time.monotonic() - t0
    check(results[g1].status == GoalStatus.COMPLETED, "A: goal one completed")
    check(results[g2].status == GoalStatus.COMPLETED, "A: goal two completed")
    check(read_cap.max_active >= 2, "A: both read steps overlapped (concurrent)")
    check(elapsed < 0.45, f"A: wall clock {elapsed:.2f}s materially below serial")
    rows = engine.scheduler_registry.list_work()
    check(len([r for r in rows if r.status == SchedulerWorkStatus.COMPLETED]) == 2,
          "A: exactly one COMPLETED registry row per dispatched step")
    engine.shutdown()
    storage.close()

    # ---------------------------------------------------------------- B -----
    print("\nB. same-resource mutations across goals obey durable FIFO")
    engine, gm, storage = _engine(
        db, sb, StepPlanner(lambda d: [_write("a.txt", content=d)]),
        write_cap=SlowWriteCapability(sb, sleep=0.15), max_concurrency=2,
        lock_wait_max_seconds=10.0)
    g1 = _submit(engine, "first")
    g2 = _submit(engine, "second")
    wc = SlowWriteCapability(sb, sleep=0.15)
    engine, gm, storage = _engine(
        db, sb, StepPlanner(lambda d: [_write("a.txt", content=d)]),
        write_cap=wc, max_concurrency=2, lock_wait_max_seconds=10.0)
    g1 = _submit(engine, "first")
    g2 = _submit(engine, "second")
    results = engine.run_goals([g1, g2])
    check(results[g1].status == GoalStatus.COMPLETED, "B: first goal completed")
    check(results[g2].status == GoalStatus.COMPLETED, "B: second goal completed")
    check(wc.max_active == 1, "B: writes never overlapped (lock serialized them)")
    check(wc.calls.count("a.txt") == 2, "B: exactly two successful mutations")
    check((sb / "a.txt").read_text(encoding="utf-8") in ("first", "second"),
          "B: last writer won exactly once (no duplicate execution)")
    waiters = storage.list_waiters()
    check(len(waiters) >= 1 and all(
        w.status == LockWaiterStatus.ACQUIRED for w in waiters),
        f"B: {len(waiters)} durable FIFO waiter(s) acquired in order")
    engine.shutdown()
    storage.close()

    # ---------------------------------------------------------------- C -----
    print("\nC. approval-pending goal consumes no worker; other goal continues")
    engine, gm, storage = _engine(
        db, sb, StepPlanner(lambda d: ([_write("a.txt")] if "write" in d
                                       else [_read("a.txt"), _read("b.txt")])),
        max_concurrency=2, approve_risk_high=True)
    g_write = _submit(engine, "write a")
    g_read = _submit(engine, "read files")
    results = engine.run_goals([g_write, g_read])
    check(results[g_read].status == GoalStatus.COMPLETED,
          "C: read goal completed while the write goal waited on approval")
    write_task = next(t for t in storage.list_tasks() if t.goal_id == g_write)
    check(write_task.status == TaskStatus.AWAITING_APPROVAL,
          "C: write goal parked durably at the approval boundary")
    running = engine.scheduler_registry.list_work(status=SchedulerWorkStatus.RUNNING)
    check(running == [], "C: no RUNNING registry row for the blocked goal")
    check(engine.scheduler.running_count() == 0, "C: no worker consumed while blocked")
    engine.shutdown()
    storage.close()

    # ---------------------------------------------------------------- D -----
    print("\nD. recovery-required goal consumes no worker; other goal continues")
    engine, gm, storage = _engine(
        db, sb, StepPlanner(lambda d: ([_write("a.txt")] if "write" in d
                                       else [_read("a.txt")])),
        write_cap=FailWrite(sb), max_concurrency=2, approve_risk_high=False)
    g_write = _submit(engine, "write a")
    engine.run_goal(g_write)
    rec = engine.recovery_store.list_recoveries()
    check(len(rec) == 1 and rec[0].status.value == "required",
          "D: write failure created durable recovery-required state")
    g_read = _submit(engine, "read a")
    results = engine.run_goals([g_write, g_read])
    check(results[g_read].status == GoalStatus.COMPLETED,
          "D: read goal completed while the write goal was recovery-gated")
    running = engine.scheduler_registry.list_work(status=SchedulerWorkStatus.RUNNING)
    check(running == [], "D: recovery-gated goal consumed no worker")
    engine.shutdown()
    storage.close()

    # ---------------------------------------------------------------- E -----
    print("\nE. restart with multiple goals: durable state, no duplicate mutation")
    db_e = tmp / "adr025e.db"
    engine, gm, storage = _engine(
        db_e, sb, StepPlanner(lambda d: ([_write("a.txt")] if "A" in d
                                         else [_write("b.txt")])),
        write_cap=SlowWriteCapability(sb, sleep=0.01), max_concurrency=2,
        approve_risk_high=False)
    g_a = _submit(engine, "goal A")
    g_b = _submit(engine, "goal B")
    engine.run_goals([g_a])          # A completes; B never ran
    check((sb / "a.txt").read_text(encoding="utf-8") == "x",
          "E: goal A's mutation completed before the 'crash'")
    engine.shutdown()
    storage.close()

    # fresh engine on the same DB == process restart (new scheduler identity)
    engine2, gm2, storage2 = _engine(
        db_e, sb, StepPlanner(lambda d: ([_write("a.txt")] if "A" in d
                                         else [_write("b.txt")])),
        write_cap=SlowWriteCapability(sb, sleep=0.01), max_concurrency=2,
        approve_risk_high=False)
    check(engine2.scheduler_id != engine.scheduler_id,
          "E: restarted engine has a fresh durable scheduler identity")
    results = engine2.run_goals([g_a, g_b])
    check(results[g_a].status == GoalStatus.COMPLETED, "E: goal A still completed")
    check(results[g_b].status == GoalStatus.COMPLETED, "E: goal B completed on restart")
    attempts = [e for e in storage2.list_events() if e.kind == "mutation.attempted"]
    check(len(attempts) == 2, "E: exactly two mutations total - A never replayed")
    check((sb / "b.txt").read_text(encoding="utf-8") == "x", "E: B written exactly once")
    abandoned = [r for r in storage2.list_work(status=SchedulerWorkStatus.ABANDONED)]
    check(abandoned == [], "E: no abandoned work left after the restart run")
    engine2.shutdown()
    storage2.close()

    # ---------------------------------------------------------------- F -----
    print("\nF. fairness: many-step goal cannot starve a one-step goal")
    for i in range(8):
        (sb / f"f{i}.txt").write_text(str(i), encoding="utf-8")
    read_cap = SlowReadCapability(sb, sleep=0.02)
    engine, gm, storage = _engine(
        db, sb, StepPlanner(lambda d: ([_read(f"f{i}.txt") for i in range(8)]
                                       if "many" in d else [_read("b.txt")])),
        read_cap=read_cap, max_concurrency=2)
    g_many = _submit(engine, "many steps goal")
    g_one = _submit(engine, "one step goal")
    results = engine.run_goals([g_many, g_one])
    check(results[g_many].status == GoalStatus.COMPLETED, "F: many-step goal completed")
    check(results[g_one].status == GoalStatus.COMPLETED, "F: one-step goal completed")
    check("b.txt" in read_cap.started[:2],
          f"F: one-step goal got a worker in the first round (start order "
          f"{read_cap.started[:4]})")
    check(read_cap.max_active <= 2, "F: global bound never exceeded (max_active=2)")
    engine.shutdown()
    storage.close()

    print("\n" + "=" * 78)
    print(f"ADR-025 demo PASSED ({CHECKS} checks) - cross-goal durable concurrency")
    print("=" * 78)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
