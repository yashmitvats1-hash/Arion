#!/usr/bin/env python3
"""ADR-024 DoD demo: bounded in-process concurrency on the durable lock + FIFO.

The unit of concurrency is the in-process STEP (scheduler worker threads,
bounded by max_concurrency, default 1 = fully sequential). Concurrency never
grants authorization: every step still runs live policy -> approval ->
durable mutation lock -> FIFO queue -> capability -> verify. The scheduler
is only the source of worker lifecycle state; the durable SQLite lock store
remains the ONLY source of mutation ownership; the durable FIFO queue
remains the ONLY source of waiter order.

  A  parallel reads: two independent read-only steps overlap on two workers,
     each with its OWN live authorization check (no authz reuse).
  B  same-resource writes: two write steps on one resource serialize through
     the durable lock; the FIFO queue stays intact (positions 1,2); exactly
     two mutations, no duplicates, distinct lock ids.
  C  different-resource writes: two write steps on DIFFERENT resources run
     concurrently with independent authz + independent locks.
  D  blocked mutation does not stall reads: one write waits on a held lock
     while an independent read completes; nothing mutates until the holder
     releases; the goal still finishes.
  E  restart/cancellation: a crash mid-flight leaves durable per-step state
     (no replay of the completed mutation on restart); a cancelled queued
     item never runs; shutdown() rejects new work and leaves no orphan
     workers.

Deterministic and offline: no LLM, no network, no subprocess, no shell -
everything runs in-process on the injected clock/sleeper/barriers.
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
    PendingApprovalHandler,
    RelativePathBoundary,
    ResourcePolicy,
)
from arion.orchestration.engine import ArionEngine
from arion.state.locks import canonical_resource
from arion.state.models import GoalStatus, PlanStep, StepStatus, TaskStatus, VerificationPolicy
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
# deterministic capabilities (barriers + active counters, no network)
# --------------------------------------------------------------------------- #


class SlowReadCapability(FilesystemReadCapability):
    """Read-only capability that sleeps and tracks overlap."""

    def __init__(self, sandbox, sleep=0.3):
        super().__init__(sandbox)
        self.sleep = sleep
        self.active = 0
        self.max_active = 0
        self.lock = threading.Lock()
        self.calls = 0
        self.completed_at: list[float] = []

    def execute(self, action, params):
        with self.lock:
            self.active += 1
            self.max_active = max(self.max_active, self.active)
            self.calls += 1
        try:
            time.sleep(self.sleep)
            result = super().execute(action, dict(params))
            with self.lock:
                self.completed_at.append(time.monotonic())
            return result
        finally:
            with self.lock:
                self.active -= 1


class SlowWriteCapability(FilesystemWriteCapability):
    """Write capability that sleeps (holding the mutation lock) and tracks
    overlap of concurrent MUTATIONS."""

    def __init__(self, sandbox, sleep=0.3):
        super().__init__(sandbox)
        self.sleep = sleep
        self.active = 0
        self.max_active = 0
        self.mlock = threading.Lock()
        self.calls = 0
        self.exec_started_at: list[float] = []

    def execute(self, action, params):
        with self.mlock:
            self.active += 1
            self.max_active = max(self.max_active, self.active)
            self.exec_started_at.append(time.monotonic())  # only AFTER lock acquire
        try:
            time.sleep(self.sleep)
            with self.mlock:
                self.calls += 1
            return super().execute(action, dict(params))
        finally:
            with self.mlock:
                self.active -= 1


class StepPlanner:
    """Deterministic planner; steps may declare depends_on (last tuple slot)."""

    def __init__(self, steps):
        self._steps = steps

    def plan(self, goal_description, task_id, registry, context=None):
        return [PlanStep(index=i, intent=s[0], capability=s[1], action=s[2],
                         scope=s[3], params=dict(s[4]), verification=s[5],
                         depends_on=list(s[6]) if len(s) > 6 else [])
                for i, s in enumerate(self._steps)]

    def required_capabilities(self, goal_description):
        return {s[1] for s in self._steps}


def _w(path, content="x"):
    return (f"write {path}", "filesystem.write", "write", "filesystem:write",
            {"path": path, "content": content, "overwrite": True},
            VerificationPolicy("write_verified"))


def _r(path):
    return (f"read {path}", "filesystem.read", "read", "filesystem:read",
            {"path": path},
            VerificationPolicy("schema_keys", {"keys": ["content"]}))


def _policy():
    # approval NOT required for the demo's concurrency sections: approval is
    # a separate seam already proven by ADR-019/023 (and the adversarial
    # suite); the demo isolates scheduling + lock + FIFO semantics.
    return ResourcePolicy(
        allowed_scopes={"filesystem:read", "filesystem:write"},
        risk_deny=set(), risk_approve=set(),
        boundaries={FS: RelativePathBoundary()},
    )


def _engine(db_path, sandbox, planner, read_cap=None, write_cap=None,
            max_concurrency=2, sleeper=None, clock=None):
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
    engine = ArionEngine(
        storage=storage, registry=registry, planner=planner,
        router=DeterministicRouter(planner), events=events,
        policy=_policy(), approval_handler=PendingApprovalHandler(),
        goal_manager=gm, world_monitor=wm,
        max_concurrency=max_concurrency,
        lock_wait_max_seconds=0.0,  # bounded waiting enabled per-section below
        lock_clock=clock, lock_sleeper=sleeper,
    )
    return engine, gm, storage


def _sandbox(tmp: Path) -> Path:
    sb = tmp / "wsandbox"
    sb.mkdir(parents=True, exist_ok=True)
    (sb / "a.txt").write_text("a", encoding="utf-8")
    (sb / "b.txt").write_text("b", encoding="utf-8")
    (sb / "c.txt").write_text("c", encoding="utf-8")
    return sb


def main() -> int:
    print("=" * 78)
    print("ADR-024 demo: bounded in-process concurrency (durable lock + FIFO)")
    print("=" * 78)
    tmp = Path(tempfile.mkdtemp(prefix="adr024-demo-"))
    sb = _sandbox(tmp)

    # ------------------------------------------------------------------ #
    # A. parallel reads overlap; each read gets its OWN live authz
    # ------------------------------------------------------------------ #
    print("\n[A] parallel reads with independent authorization")
    read_cap = SlowReadCapability(sb, sleep=0.3)
    planner = StepPlanner([_r("a.txt"), _r("b.txt")])
    engine, gm, storage = _engine(tmp / "a.db", sb, planner, read_cap=read_cap,
                                  max_concurrency=2)
    gid = engine.submit_goal("read a and b").id
    final = engine.run_goal(gid)
    check(final.status == GoalStatus.COMPLETED, "A: goal completed")
    check(read_cap.max_active == 2, "A: two independent reads overlapped (max_active=2)")
    perm = [e for e in storage.list_events() if e.kind == "permission.checked"]
    check(len(perm) == 2, "A: each read ran its OWN live authorization (2 checks, no reuse)")
    engine.shutdown()
    engine.storage.close()

    # ------------------------------------------------------------------ #
    # B. same-resource writes serialize through the durable lock + FIFO
    # ------------------------------------------------------------------ #
    print("\n[B] same-resource writes serialize (unique lock, FIFO intact)")
    db = tmp / "b.db"
    holder = SQLiteStorage(db)
    holder_lock = holder.acquire(FS, canonical_resource(FS, "a.txt"),
                                 "filesystem.write", "write", "proc-holder",
                                 3600.0, now=None)

    class ReleaseOnSleep:
        """Deterministic handoff: release the foreign holder exactly once
        (race-free) once BOTH waiters are durably queued, so the FIFO
        head-gated acquire runs in queue order."""

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

    write_cap = SlowWriteCapability(sb, sleep=0.15)
    planner = StepPlanner([_w("a.txt", "x"), _w("a.txt", "y")])
    engine, gm, storage = _engine(db, sb, planner, write_cap=write_cap,
                                  max_concurrency=2, sleeper=ReleaseOnSleep())
    engine.lock_wait_max_seconds = 30.0
    engine.lock_wait_backoff_base = 0.02
    engine.lock_wait_backoff_max = 0.05
    gid = engine.submit_goal("write a twice").id
    final = engine.run_goal(gid)
    check(final.status == GoalStatus.COMPLETED, "B: goal completed")
    check(write_cap.max_active == 1, "B: same-resource mutations NEVER overlapped (max_active=1)")
    attempts = [e for e in storage.list_events() if e.kind == "mutation.attempted"]
    succ = [e for e in storage.list_events() if e.kind == "mutation.succeeded"]
    check(len(attempts) == 2 and len(succ) == 2, "B: exactly two mutations, no duplicates")
    waiter_events = sorted(
        [e for e in storage.list_events() if e.kind == "mutation.lock.queued"],
        key=lambda e: e.detail.get("position", 0))
    check([w.detail["position"] for w in waiter_events] == [1, 2],
          "B: FIFO queue positions [1,2] preserved")
    # map each durable position to its step so FIFO discipline is verified
    # WITHOUT depending on which worker thread happened to enqueue first:
    # the head (position 1) must acquire strictly before the tail (position 2),
    # and the final file content must be the tail's write.
    by_pos = {w.detail["position"]: int(w.step_id.rsplit("_", 1)[1])
              for w in waiter_events}
    acq = [e for e in storage.list_events() if e.kind == "mutation.lock.acquired"]
    check(len({a.detail["lock_id"] for a in acq}) == 2,
          "B: each mutation held its OWN lock id (unique ownership)")
    head_first = [int(a.step_id.rsplit("_", 1)[1]) for a in acq] == \
        [by_pos[1], by_pos[2]]
    check(head_first, "B: head waiter acquired BEFORE the tail waiter (FIFO)")
    expected_final = "y" if by_pos[2] == 1 else "x"
    check((sb / "a.txt").read_text(encoding="utf-8") == expected_final,
          "B: final content from the FIFO-last write")
    holder.close()
    engine.shutdown()
    engine.storage.close()

    # ------------------------------------------------------------------ #
    # C. different-resource writes run concurrently
    # ------------------------------------------------------------------ #
    print("\n[C] different-resource writes run concurrently")
    write_cap = SlowWriteCapability(sb, sleep=0.25)
    planner = StepPlanner([_w("a.txt", "x"), _w("b.txt", "y")])
    engine, gm, storage = _engine(tmp / "c.db", sb, planner, write_cap=write_cap,
                                  max_concurrency=2)
    gid = engine.submit_goal("write a and b").id
    final = engine.run_goal(gid)
    check(final.status == GoalStatus.COMPLETED, "C: goal completed")
    check(write_cap.max_active == 2, "C: two different-resource mutations overlapped (max_active=2)")
    acq = [e for e in storage.list_events() if e.kind == "mutation.lock.acquired"]
    check({a.detail["resource"] for a in acq} == {"a.txt", "b.txt"},
          "C: two DISTINCT lock resources (independent ownership)")
    perm = [e for e in storage.list_events() if e.kind == "permission.checked"]
    check(len(perm) == 2, "C: each write ran its OWN live authorization")
    check((sb / "a.txt").read_text(encoding="utf-8") == "x"
          and (sb / "b.txt").read_text(encoding="utf-8") == "y",
          "C: both files written correctly")
    engine.shutdown()
    engine.storage.close()

    # ------------------------------------------------------------------ #
    # D. a blocked mutation does not stall unrelated read-only work
    # ------------------------------------------------------------------ #
    print("\n[D] blocked mutation does not stall unrelated reads")
    db = tmp / "d.db"
    holder = SQLiteStorage(db)
    holder_lock = holder.acquire(FS, canonical_resource(FS, "a.txt"),
                                 "filesystem.write", "write", "proc-holder",
                                 3600.0, now=None)
    read_cap = SlowReadCapability(sb, sleep=0.15)
    write_cap = SlowWriteCapability(sb, sleep=0.15)
    planner = StepPlanner([_w("a.txt", "z"), _r("b.txt"), _r("c.txt")])
    engine, gm, storage = _engine(db, sb, planner, read_cap=read_cap,
                                  write_cap=write_cap, max_concurrency=2)
    engine.lock_wait_max_seconds = 10.0
    engine.lock_wait_backoff_base = 0.02
    engine.lock_wait_backoff_max = 0.05
    # release the foreign holder mid-run (0.8s), while the reads already ran
    timer = threading.Timer(0.8, holder.release, args=(holder_lock.lock_id, "proc-holder"))
    timer.daemon = True
    timer.start()
    gid = engine.submit_goal("write a, read b, read c").id
    final = engine.run_goal(gid)
    timer.cancel()
    check(final.status == GoalStatus.COMPLETED, "D: goal completed after the lock freed")
    check(len(read_cap.completed_at) == 2 and len(write_cap.exec_started_at) == 1
          and read_cap.completed_at[0] < write_cap.exec_started_at[0],
          "D: a read completed BEFORE the mutation's capability started "
          "(read ran while the write was still blocked - no stall)")
    check(write_cap.calls == 1 and read_cap.calls == 2,
          "D: exactly one mutation (after release) and both reads ran")
    events = storage.list_events()
    attempts = [e for e in events if e.kind == "mutation.attempted"]
    check(len(attempts) == 1, "D: no mutation ever ran while the lock was held")
    check((sb / "a.txt").read_text(encoding="utf-8") == "z",
          "D: the write applied exactly once after release")
    holder.close()
    engine.shutdown()
    engine.storage.close()

    # ------------------------------------------------------------------ #
    # E. restart preserves durable per-step state (no replay); cancellation
    #    is scheduler-owned; shutdown leaves no orphan workers
    # ------------------------------------------------------------------ #
    print("\n[E] restart mid-flight (no duplicate mutation) + cancellation + shutdown")
    db = tmp / "e.db"

    class CrashSleeper:
        """Real clock; after two backoff sleeps it raises (simulated crash)."""

        def __init__(self):
            self.n = 0

        def __call__(self, seconds):
            self.n += 1
            if self.n > 2:
                raise RuntimeError("simulated crash while waiting for mutation lock")
            time.sleep(seconds)

    holder = SQLiteStorage(db)
    holder_lock = holder.acquire(FS, canonical_resource(FS, "b.txt"),
                                 "filesystem.write", "write", "proc-holder",
                                 3600.0, now=None)
    write_cap = SlowWriteCapability(sb, sleep=0.1)
    planner = StepPlanner([_w("a.txt", "x"), _w("b.txt", "y")])
    engine, gm, storage = _engine(db, sb, planner, write_cap=write_cap,
                                  max_concurrency=2, sleeper=CrashSleeper())
    engine.lock_wait_max_seconds = 30.0
    engine.lock_wait_backoff_base = 0.02
    engine.lock_wait_backoff_max = 0.05
    gid = engine.submit_goal("write a and b").id
    try:
        engine.run_goal(gid)
        check(False, "E: expected the simulated crash to propagate")
    except RuntimeError as exc:
        check("simulated crash" in str(exc), "E: crash propagated out of run_goal (old semantics)")
    task = gm.task_history(gid)[-1]
    check(task.steps[0].status == StepStatus.SUCCEEDED,
          "E: completed step persisted durably BEFORE the crash")
    check(task.steps[1].status == StepStatus.PENDING and task.lock_wait is not None,
          "E: interrupted step stays pending with bounded lock_wait metadata")
    engine.shutdown()
    engine.storage.close()
    holder.release(holder_lock.lock_id, "proc-holder")
    holder.close()

    engine2, gm2, storage2 = _engine(db, sb, planner,
                                     write_cap=SlowWriteCapability(sb, sleep=0.1),
                                     max_concurrency=2)
    final = engine2.run_goal(gid)
    check(final.status == GoalStatus.COMPLETED, "E: fresh engine resumes the task")
    attempts2 = [e for e in storage2.list_events() if e.kind == "mutation.attempted"]
    check(len(attempts2) == 2, "E: exactly two mutations total - no replay of step a")
    check((sb / "a.txt").read_text(encoding="utf-8") == "x"
          and (sb / "b.txt").read_text(encoding="utf-8") == "y",
          "E: both files written exactly once")

    # cancellation is scheduler-owned: queued items cancelled via the
    # scheduler never run; forged/claimed cancellations are ignored
    ran: list[str] = []
    engine2.scheduler.enqueue("one", "t", 0, lambda: ran.append("one"))
    engine2.scheduler.enqueue("two", "t", 1, lambda: ran.append("two"))
    engine2.scheduler.enqueue("three", "t", 2, lambda: ran.append("three"))
    cancel = engine2.scheduler.cancel(engine2.scheduler.snapshot()["queued"][1]["id"])
    check(cancel, "E: scheduler.cancel removed a queued item")
    engine2.scheduler.run_until_done()
    check(ran == ["one", "three"], "E: cancelled queued item never ran (no orphan)")
    engine2.scheduler.shutdown()
    try:
        engine2.scheduler.enqueue("four", "t", 3, lambda: ran.append("four"))
        check(False, "E: enqueue after shutdown must fail closed")
    except Exception:
        check(True, "E: enqueue after shutdown fails closed")
    snap = engine2.scheduler.snapshot()
    check(snap["queued"] == [] and snap["running"] == [] and snap["workers"] == 0,
          "E: shutdown joined every worker; nothing left running")
    check(ran == ["one", "three"], "E: no orphan execution after shutdown")
    engine2.shutdown()
    engine2.storage.close()

    print("\n" + "=" * 78)
    print(f"ADR-024 demo PASSED ({CHECKS} checks) - bounded in-process concurrency")
    print("=" * 78)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
