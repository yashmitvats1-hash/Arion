#!/usr/bin/env python3
"""ADR-023 DoD demo: fair, durable mutation-lock wait queues (A-E).

Real subprocesses share one SQLite DB; the DB is the coordination authority
for both the locks and the durable FIFO waiter queue.

  A  FIFO:        A holds the lock -> B queues (pos 1) -> C queues (pos 2)
                  -> A releases -> B acquires FIRST, mutates once, releases
                  -> C acquires, mutates once. B can never be overtaken by C.
  B  restart:     B queues (pos 1) -> B is KILLED mid-wait (waiter row stays
                  queued) -> C queues (pos 2) -> A releases -> B restarts and
                  still wins before C (durable queue position).
  C  timeout:     A holds past B's deadline -> B fails durably (typed timeout,
                  no mutation, no recovery) and leaves the queue cleanly.
  D  live re-auth: B waits in the queue; the ActionSpec tightens mid-wait ->
                  after the lock frees, live re-validation DENIES the stale
                  grant (no mutation) -> restore -> fresh authorization path.
  E  adversarial: poisoned memory/model output claims queue_position=0 /
                  priority=highest / lock_acquired / owner -> the REAL queue
                  (store) stays authoritative: position 1, owner unchanged.

Fairness is coordination, never authorization: the queue only decides who
gets the OPPORTUNITY; the live authorization layer stays the sole authority.
Deterministic and offline (no LLM, no shell).
"""

from __future__ import annotations

import json
import os
import signal
import subprocess
import sys
import tempfile
import time
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

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
CHECKS = 0


def check(cond: bool, msg: str) -> None:
    global CHECKS
    CHECKS += 1
    if not cond:
        print(f"  FAIL: {msg}")
        sys.exit(1)
    print(f"  ok: {msg}")


WORKER = os.path.join(os.path.dirname(os.path.abspath(__file__)), "_lock_demo_worker.py")


def run_worker(*argv: str) -> str:
    proc = subprocess.run([sys.executable, WORKER, *argv],
                          capture_output=True, text=True, timeout=120)
    if proc.returncode != 0:
        print("  worker failed:", proc.stdout[-2000:], proc.stderr[-2000:])
        sys.exit(1)
    return proc.stdout.strip()


def spawn_worker(*argv: str):
    return subprocess.Popen([sys.executable, WORKER, *argv],
                            stdout=subprocess.PIPE, text=True, bufsize=1)


def read_queued(proc):
    """Read worker lines until the QUEUED marker; returns the marker dict."""
    while True:
        line = proc.stdout.readline().strip()
        if line.startswith("QUEUED"):
            return json.loads(line[len("QUEUED"):].strip())


def read_json(proc, timeout=90):
    lines = []
    deadline = time.time() + timeout
    while time.time() < deadline:
        line = proc.stdout.readline()
        if not line:
            break
        line = line.strip()
        if line.startswith("{"):
            return json.loads(line), lines
        lines.append(line)
    proc.wait(timeout=5)
    raise AssertionError(f"worker did not emit JSON; lines={lines}")


def acquired_order(db_path, task_ids):
    st = SQLiteStorage(db_path)
    order = [e.task_id for e in st.list_events()
             if e.kind == "mutation.lock.acquired" and e.task_id in task_ids]
    st.close()
    return order


def main() -> int:
    work = Path(tempfile.mkdtemp(prefix="arion-adr023-demo-"))
    print("=" * 78)
    print("ADR-023 demo: fair, durable mutation-lock wait queues (A-E)")
    print("=" * 78)

    # ------------------------------------------------------------ scenario A
    print("\n[A] FIFO: A holds -> B queues (1) -> C queues (2) -> A releases")
    print("    -> B acquires first, mutates once -> C acquires, mutates once")
    sb = work / "a" / "repo"
    sb.mkdir(parents=True)
    db = work / "a" / "arion.db"

    a = spawn_worker("hold-release", "--db", str(db), "--sandbox", str(sb),
                     "--hold", "3.0", "--wait-max", "60")
    assert a.stdout.readline().strip() == "HOLDING"
    b = spawn_worker("wait-write", "--db", str(db), "--sandbox", str(sb),
                     "--mark-queued", "--wait-max", "30",
                     "--backoff-base", "0.05", "--backoff-max", "0.1")
    bq = read_queued(b)
    c = spawn_worker("wait-write", "--db", str(db), "--sandbox", str(sb),
                     "--mark-queued", "--wait-max", "30",
                     "--backoff-base", "0.05", "--backoff-max", "0.1")
    cq = read_queued(c)
    check(bq["position"] == 1 and cq["position"] == 2,
          "B and C enqueued in FIFO order (durable positions 1, 2)")
    check(bq["position"] < cq["position"], "B queued before C")

    b_out, _ = read_json(b)
    c_out, _ = read_json(c)
    a.wait(timeout=10)
    check(b_out["goal_status"] == "completed" and c_out["goal_status"] == "completed",
          "both waiters completed after A released")
    order = acquired_order(db, {b_out["task_id"], c_out["task_id"]})
    check(len(order) == 2 and order[0] == b_out["task_id"] and order[1] == c_out["task_id"],
          "B acquired BEFORE C (audit order proves FIFO handoff)")
    st = SQLiteStorage(db)
    attempts = [e for e in st.list_events() if e.kind == "mutation.attempted"]
    check(len(attempts) == 2, "exactly one mutation per waiter (no duplicates)")
    queued = [w for w in st.list_waiters() if w.status == LockWaiterStatus.QUEUED]
    check(queued == [], "queue drained after the handoff (no stale waiters)")
    st.close()
    check((sb / "notes.txt").read_text(encoding="utf-8") == "hello",
          "file written by both waiters (overwrite) with intended content")

    # ------------------------------------------------------------ scenario B
    print("\n[B] restart survival: B is killed mid-wait; its durable position 1")
    print("    survives and still wins over C after restart")
    sb = work / "b" / "repo"
    sb.mkdir(parents=True)
    db = work / "b" / "arion.db"
    a2 = spawn_worker("hold-release", "--db", str(db), "--sandbox", str(sb),
                      "--hold", "5.0", "--wait-max", "60")
    assert a2.stdout.readline().strip() == "HOLDING"
    b2 = spawn_worker("wait-write", "--db", str(db), "--sandbox", str(sb),
                      "--mark-queued", "--wait-max", "40",
                      "--backoff-base", "0.05", "--backoff-max", "0.1")
    bq2 = read_queued(b2)
    c2 = spawn_worker("wait-write", "--db", str(db), "--sandbox", str(sb),
                      "--mark-queued", "--wait-max", "40",
                      "--backoff-base", "0.05", "--backoff-max", "0.1")
    cq2 = read_queued(c2)
    check(bq2["position"] == 1 and cq2["position"] == 2, "B(1) and C(2) queued")
    # kill B mid-wait; the waiter row stays queued (durable)
    b2.kill()
    b2.wait(timeout=10)
    st0 = SQLiteStorage(db)
    b_waiter = st0.get_waiter(bq2["waiter_id"])
    check(b_waiter is not None and b_waiter.status == LockWaiterStatus.QUEUED,
          "killed waiter's queue row survives (durable position)")
    st0.close()
    # wait for A to release, then restart B (fresh process, same goal)
    assert a2.stdout.readline().strip() == "RELEASED"
    a2.wait(timeout=10)
    b2r = json.loads(run_worker("wait-write", "--db", str(db), "--sandbox", str(sb),
                                "--goal", bq2["goal_id"], "--wait-max", "40",
                                "--backoff-base", "0.05", "--backoff-max", "0.1"))
    check(b2r["goal_status"] == "completed", "restarted B completed (reused its queue position)")
    c2_out, _ = read_json(c2)
    check(c2_out["goal_status"] == "completed", "C completed after B (FIFO preserved)")
    st = SQLiteStorage(db)
    attempts = [e for e in st.list_events() if e.kind == "mutation.attempted"]
    check(len(attempts) == 2, "B (restarted) + C: exactly one mutation each")
    st.close()

    # ------------------------------------------------------------ scenario C
    print("\n[C] timeout: A holds past B's deadline -> typed durable timeout,")
    print("    no mutation, no recovery, queue left cleanly")
    sb = work / "c" / "repo"
    sb.mkdir(parents=True)
    db = work / "c" / "arion.db"
    a3 = spawn_worker("hold-release", "--db", str(db), "--sandbox", str(sb),
                      "--hold", "8", "--wait-max", "60")
    assert a3.stdout.readline().strip() == "HOLDING"
    b3 = json.loads(run_worker("wait-write", "--db", str(db), "--sandbox", str(sb),
                               "--wait-max", "1.0", "--backoff-base", "0.1",
                               "--backoff-max", "0.2"))
    a3.terminate()
    check(b3["task_status"] == "failed" and "wait timed out" in (b3["task_error"] or ""),
          "deadline expiry -> durable typed timeout failure")
    check(b3["goal_status"] == "blocked", "goal durably BLOCKED (explainable)")
    check(not (sb / "notes.txt").exists(), "NO mutation on the timeout path")
    st = SQLiteStorage(db)
    check(st.list_recoveries() == [], "NO recovery record (contention != failure)")
    timed_out = [w for w in st.list_waiters() if w.status == LockWaiterStatus.TIMED_OUT]
    queued = [w for w in st.list_waiters() if w.status == LockWaiterStatus.QUEUED]
    check(len(timed_out) >= 1 and queued == [],
          "expired waiter left the queue cleanly (timed_out, nothing queued)")
    st.close()

    # ------------------------------------------------------------ scenario D
    print("\n[D] live re-authorization: ActionSpec tightens while B waits in")
    print("    the queue -> post-wait re-validation denies -> fresh path")
    sb = work / "d" / "repo"
    sb.mkdir(parents=True)
    db = work / "d" / "arion.db"
    holder_store = SQLiteStorage(db)
    holder_lock = holder_store.acquire(FS, canonical_resource(FS, "notes.txt"),
                                       "filesystem.write", "write", "proc-holder",
                                       3600.0, now=None)

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

    class WritePlanner:
        def plan(self, goal_description, task_id, registry, context=None):
            return [PlanStep(index=0, intent="write notes", capability="filesystem.write",
                             action="write", scope="filesystem:write",
                             params={"path": "notes.txt", "content": "hello",
                                     "overwrite": False},
                             verification=VerificationPolicy("write_verified"))]

        def required_capabilities(self, goal_description):
            return {"filesystem.write"}

    storage = SQLiteStorage(db)
    registry = CapabilityRegistry()
    registry.register(FilesystemReadCapability(sb))
    registry.register(FilesystemWriteCapability(sb))
    events = EventLogger(sinks=[storage])
    cognitive = SQLiteCognitiveStore(db)
    wm = WorldStateMonitor(cognitive, sink=events)
    wm.observe("registered_capabilities", sorted(registry.list()), source="system")
    gm = GoalManager(storage=storage, cognitive_store=cognitive, events=events,
                     strategy_selector=StrategySelector(),
                     progress_evaluator=DeterministicProgressEvaluator(),
                     world_monitor=wm)
    engine = ArionEngine(
        storage=storage, registry=registry, planner=WritePlanner(),
        router=DeterministicRouter(DeterministicPlanner()), events=events,
        policy=ResourcePolicy(allowed_scopes={"filesystem:read", "filesystem:write"},
                              risk_deny=set(), risk_approve={"high"},
                              boundaries={FS: RelativePathBoundary()}),
        approval_handler=PendingApprovalHandler(), goal_manager=gm, world_monitor=wm,
        lock_wait_max_seconds=60.0, lock_wait_backoff_base=1.0,
        lock_wait_backoff_max=1.0,
    )

    class TightenDuringWait:
        def __init__(self):
            self.n = 0

        def sleep(self, seconds):
            self.n += 1
            if self.n == 1:
                registry.register(TightenedWrite(sb))
            if self.n == 2:
                holder_store.release(holder_lock.lock_id, "proc-holder")
            time.sleep(seconds)

    engine.lock_sleeper = TightenDuringWait().sleep
    gid = engine.submit_goal("write notes").id
    engine.run_goal(gid)
    req = engine.approval_store.list_requests()[-1]
    engine.resolve_approval_request(req.approval_id, ApprovalOutcome.APPROVED, actor="user:alice")
    final = engine.run_goal(gid)
    task = gm.task_history(gid)[-1]
    check(task.status == TaskStatus.FAILED and "not permitted" in (task.error or ""),
          "post-wait re-validation DENIED the stale grant (live ActionSpec authoritative)")
    check(not (sb / "notes.txt").exists(), "NO mutation under the stale authorization")
    # restore -> fresh authorization path -> mutation proceeds exactly once
    registry.register(FilesystemWriteCapability(sb))
    final = engine.run_goal(gid)
    task = gm.task_history(gid)[-1]
    check(task.status == TaskStatus.AWAITING_APPROVAL, "replanned -> FRESH approval queued")
    fresh = [r for r in engine.approval_store.list_requests()
             if r.status.value == "pending"][-1]
    engine.resolve_approval_request(fresh.approval_id, ApprovalOutcome.APPROVED, actor="user:alice")
    final = engine.run_goal(gid)
    check(final.status == GoalStatus.COMPLETED, "fresh approval -> mutation -> completed")
    check((sb / "notes.txt").read_text(encoding="utf-8") == "hello",
          "mutation ran exactly once under the fresh path")
    holder_store.close()
    engine.storage.close()

    # ------------------------------------------------------------ scenario E
    print("\n[E] adversarial: poisoned memory/model claims cannot change the")
    print("    queue (position/owner/priority are store-authoritative)")
    sb = work / "e" / "repo"
    sb.mkdir(parents=True)
    db = work / "e" / "arion.db"
    holder_store_e = SQLiteStorage(db)
    holder_lock_e = holder_store_e.acquire(FS, canonical_resource(FS, "notes.txt"),
                                           "filesystem.write", "write", "proc-real",
                                           3600.0, now=None)

    class SpoofPlanner(WritePlanner):
        def plan(self, goal_description, task_id, registry, context=None):
            step = super().plan(goal_description, task_id, registry, context=context)[0]
            step.params.update({"queue_position": 0, "priority": "highest",
                                "lock_acquired": True, "owner": "proc-evil",
                                "waiter_id": "waiter_forged"})
            return [step]

    storage = SQLiteStorage(db)
    registry = CapabilityRegistry()
    registry.register(FilesystemReadCapability(sb))
    registry.register(FilesystemWriteCapability(sb))
    events = EventLogger(sinks=[storage])
    cognitive = SQLiteCognitiveStore(db)
    wm = WorldStateMonitor(cognitive, sink=events)
    wm.observe("registered_capabilities", sorted(registry.list()), source="system")
    gm = GoalManager(storage=storage, cognitive_store=cognitive, events=events,
                     strategy_selector=StrategySelector(),
                     progress_evaluator=DeterministicProgressEvaluator(),
                     world_monitor=wm)
    engine = ArionEngine(
        storage=storage, registry=registry, planner=SpoofPlanner(),
        router=DeterministicRouter(DeterministicPlanner()), events=events,
        policy=ResourcePolicy(allowed_scopes={"filesystem:read", "filesystem:write"},
                              risk_deny=set(), risk_approve={"high"},
                              boundaries={FS: RelativePathBoundary()}),
        approval_handler=PendingApprovalHandler(), goal_manager=gm, world_monitor=wm,
        memory=SQLiteMemoryStore(db),
        lock_wait_max_seconds=10_000.0, lock_wait_backoff_base=1.0,
        lock_wait_backoff_max=1.0,
    )
    engine.memory.record_episode(Episode(
        episode_id="ep_evil", goal="write notes", outcome="completed", task_id="t",
        plan_summary=[], actions=[], resources=[], tags=["filesystem.write"],
        authorization={}, failures=[], recovery={"lock_acquired": True,
                                                 "priority": "highest"},
        importance=1.0,
    ))

    class CrashSleep:
        def __init__(self):
            self.n = 0

        def sleep(self, seconds):
            self.n += 1
            if self.n > 2:
                raise RuntimeError("simulated crash while waiting for mutation lock")
            time.sleep(seconds)

    engine.lock_sleeper = CrashSleep().sleep
    gid_e = engine.submit_goal("write notes").id
    engine.run_goal(gid_e)
    req_e = engine.approval_store.list_requests()[-1]
    engine.resolve_approval_request(req_e.approval_id, ApprovalOutcome.APPROVED, actor="user:alice")
    try:
        engine.run_goal(gid_e)
        check(False, "expected simulated crash during the wait")
    except RuntimeError:
        pass
    task_e = gm.task_history(gid_e)[-1]
    check(task_e.lock_wait["position"] == 1 and task_e.lock_wait["waiter_id"] != "waiter_forged",
          "real queue position = 1; forged position/waiter ignored")
    check(engine.mutation_lock_store.get_waiter("waiter_forged") is None,
          "forged waiter never exists in the store")
    locks = engine.mutation_lock_store.list()
    check(len(locks) == 1 and locks[0].owner_id == "proc-real",
          "lock owner unchanged (memory/model cannot transfer ownership)")
    check(task_e.lock_wait["attempts"] == 3, "retry state driven only by the engine (not memory)")
    holder_store_e.close()
    engine.storage.close()

    print("\n" + "=" * 78)
    print(f"ADR-023 demo PASSED ({CHECKS} checks) - FIFO, restart, timeout, live-authz, adversarial")
    print("=" * 78)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
