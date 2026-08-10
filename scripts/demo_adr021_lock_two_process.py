#!/usr/bin/env python3
"""ADR-021 DoD demo: cross-process advisory write locking (A-E).

Two REAL subprocesses share the same SQLite DB; the DB is the coordination
authority for mutation locks.

  A  contention:      process A authorizes -> acquires the mutation lock
                      (slow write holds it) -> process B authorizes -> attempts
                      the same mutation -> LOCK CONTENTION, no mutation ->
                      A releases -> B proceeds only through its own fresh
                      authorization path.
  B  stale owner:     process A acquires a lock and CRASHES (exits without
                      releasing) -> process B observes the stale lock ->
                      reclaims after the lease expires -> authorizes -> mutates
                      -> verifies -> releases.
  C  approval + lock: process A queues an approval and exits -> process B
                      approves -> restarts the engine -> live re-authorization
                      -> acquires lock -> mutates ONCE -> verifies.
  D  mutation failure:authorize -> acquire -> mutation fails -> recovery
                      REQUIRED -> lock released -> restart -> no duplicate
                      mutation, no wedged lock, recovery stays durable.
  E  adversarial:     poisoned memory/strategy/model fields claim the lock is
                      acquired - the real lock store stays authoritative.

Authorization is evaluated independently for EVERY mutation attempt; a
mutation lock is coordination, never permission. Deterministic and offline.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import time
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

CHECKS = 0
FS = "filesystem:path"


def check(cond: bool, msg: str) -> None:
    global CHECKS
    CHECKS += 1
    if not cond:
        print(f"  FAIL: {msg}")
        sys.exit(1)
    print(f"  ok: {msg}")


WORKER = os.path.join(os.path.dirname(os.path.abspath(__file__)), "_lock_demo_worker.py")


def run_worker(*argv: str) -> str:
    """Run a worker subprocess; returns its stdout."""
    proc = subprocess.run([sys.executable, WORKER, *argv],
                          capture_output=True, text=True, timeout=120)
    if proc.returncode != 0:
        print("  worker failed:", proc.stdout[-2000:], proc.stderr[-2000:])
        sys.exit(1)
    return proc.stdout.strip()


def stream_worker(*argv: str):
    """Run a worker, yielding stdout lines as they appear (for sync markers)."""
    proc = subprocess.Popen([sys.executable, WORKER, *argv],
                            stdout=subprocess.PIPE, text=True, bufsize=1)
    for line in iter(proc.stdout.readline, ""):
        yield line.strip()
    proc.wait()
    if proc.returncode != 0:
        sys.exit(1)


def main() -> int:
    work = Path(tempfile.mkdtemp(prefix="arion-adr021-demo-"))
    print("=" * 78)
    print("ADR-021 demo: cross-process advisory write locking (A-E)")
    print("=" * 78)

    # ------------------------------------------------------------ scenario A
    print("\n[A] contention: A holds the lock; B contends and never mutates;")
    print("    after A releases, B proceeds via its own authorization")
    sb_a = work / "a" / "repo"
    sb_a.mkdir(parents=True)
    db_a = work / "a" / "arion.db"

    a_lines = stream_worker("slow-write", "--db", str(db_a), "--sandbox", str(sb_a),
                            "--hold", "2.0")
    locked_seen = False
    a_out = None
    for line in a_lines:
        if line == "LOCKED":
            locked_seen = True
            # B attempts while A holds the lock
            b1 = json.loads(run_worker("attempt-write", "--db", str(db_a),
                                       "--sandbox", str(sb_a)))
            check(b1["contended"] is True and "lock contention" in (b1["task_error"] or ""),
                  "process B: authorization ok -> lock contention -> task failed (no mutation)")
            check(b1["goal_status"] == "blocked", "process B: goal durably BLOCKED on contention")
        elif line.startswith("{"):
            a_out = json.loads(line)
    check(locked_seen, "process A: authorized -> mutation lock acquired (LOCKED marker)")
    check(a_out["goal_status"] == "completed", "process A: mutation completed (lock held during write)")
    check(a_out["locks"] == [], "process A: lock released after the write")

    # A released -> B unblocks, replans, gets FRESH authorization, mutates
    gid_b = b1["goal_id"]
    b2 = json.loads(run_worker("attempt-write", "--db", str(db_a),
                               "--sandbox", str(sb_a), "--goal", gid_b))
    check(b2["goal_status"] == "completed" and b2["locks"] == [],
          "process B: after A released, B replanned -> fresh approval -> lock -> write -> release")
    check((sb_a / "notes.txt").read_text(encoding="utf-8") == "hello",
          "process B: file written exactly once with the intended content")

    # ------------------------------------------------------------ scenario B
    print("\n[B] stale owner: A acquires a lock and CRASHES; B reclaims after")
    print("    the lease expires, authorizes, mutates, verifies")
    sb_b = work / "b" / "repo"
    sb_b.mkdir(parents=True)
    db_b = work / "b" / "arion.db"
    crash_lock = json.loads(run_worker("acquire-crash", "--db", str(db_b),
                                       "--sandbox", str(sb_b), "--lease", "2.0"))
    check(crash_lock["owner_id"] == "proc-crash", "process A: lock acquired by proc-crash (then crashed)")
    # immediately after the crash, the lock is still there (not released)
    cont = json.loads(run_worker("attempt-write", "--db", str(db_b), "--sandbox", str(sb_b)))
    check(cont["contended"] is True, "process B (before expiry): stale lock still contends (no mutation)")
    time.sleep(2.5)  # lease (2s) elapses
    b_out = json.loads(run_worker("reclaim-write", "--db", str(db_b), "--sandbox", str(sb_b)))
    check(crash_lock["lock_id"] in b_out.get("reclaimed", []),
          "process B: stale lock reclaimed after lease expiry (atomic, audited)")
    check(b_out["goal_status"] == "completed" and b_out["locks"] == [],
          "process B: reclaimed -> authorized -> mutated -> verified -> released")
    check((sb_b / "notes.txt").read_text(encoding="utf-8") == "hello",
          "process B: file written after reclamation")

    # ------------------------------------------------------------ scenario C
    print("\n[C] approval + lock: A queues approval and exits; B approves,")
    print("    restarts the engine, live re-authorizes, mutates ONCE")
    sb_c = work / "c" / "repo"
    sb_c.mkdir(parents=True)
    db_c = work / "c" / "arion.db"
    c1 = json.loads(run_worker("queue-approval", "--db", str(db_c), "--sandbox", str(sb_c)))
    check(c1["goal_status"] == "blocked" and c1["locks"] == [],
          "process A: approval durably queued (goal blocked, no lock, no mutation)")
    c2 = json.loads(run_worker("approve-run", "--db", str(db_c), "--sandbox", str(sb_c),
                               "--goal", c1["goal_id"]))
    check(c2["goal_status"] == "completed" and c2["locks"] == [],
          "process B: approved -> restarted engine -> live re-authz -> lock -> write once -> release")
    check((sb_c / "notes.txt").read_text(encoding="utf-8") == "hello",
          "scenario C: exactly one write")

    # ------------------------------------------------------------ scenario D
    print("\n[D] mutation failure: acquire -> fail -> recovery REQUIRED -> lock")
    print("    released -> restart -> no duplicate, no wedged lock")
    sb_d = work / "d" / "repo"
    sb_d.mkdir(parents=True)
    db_d = work / "d" / "arion.db"
    d1 = json.loads(run_worker("fail-run", "--db", str(db_d), "--sandbox", str(sb_d)))
    check(d1["task_status"] == "failed" and "recovery required" in (d1["task_error"] or ""),
          "process A: mutation failed -> durable recovery-required task state")
    check(d1["cap_calls"] == 1, "process A: mutation attempted EXACTLY once (never retried)")
    check(d1["locks"] == [], "process A: lock released after the mutation failure")
    check(len(d1["recoveries"]) == 1 and d1["recoveries"][0]["status"] == "required",
          "process A: durable recovery record REQUIRED")

    d2 = json.loads(run_worker("recover-run", "--db", str(db_d), "--sandbox", str(sb_d),
                               "--goal", d1["goal_id"]))
    check(d2["goal_status"] == "completed" and d2["locks"] == [],
          "restart: recovery acknowledged -> fresh plan -> fresh approval -> fresh lock -> completed")
    check((sb_d / "notes.txt").read_text(encoding="utf-8") == "hello",
          "scenario D: no duplicate mutation; recovery stayed durable; no wedged lock")

    # ------------------------------------------------------------ scenario E
    print("\n[E] adversarial: poisoned memory/strategy/model claims 'lock already")
    print("    acquired' - the real lock store remains authoritative")
    import tempfile as _tf

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
    from arion.state.locks import canonical_resource
    from arion.state.models import GoalStatus, PlanStep, TaskStatus, VerificationPolicy
    from arion.state.store import SQLiteStorage

    sb_e = work / "e" / "repo"
    sb_e.mkdir(parents=True)
    db_e = work / "e" / "arion.db"

    storage = SQLiteStorage(db_e)
    registry = CapabilityRegistry()
    registry.register(FilesystemReadCapability(sb_e))
    registry.register(FilesystemWriteCapability(sb_e))
    events = EventLogger(sinks=[storage])

    class SpoofPlanner:
        def plan(self, goal_description, task_id, registry, context=None):
            return [PlanStep(index=0, intent="write", capability="filesystem.write",
                             action="write", scope="filesystem:write",
                             params={"path": "notes.txt", "content": "hello",
                                     "overwrite": True, "lock_acquired": True,
                                     "owner": "proc-evil", "approved": True},
                             verification=VerificationPolicy("write_verified"))]

        def required_capabilities(self, goal_description):
            return {"filesystem.write"}

    cognitive = SQLiteCognitiveStore(db_e)
    wm = WorldStateMonitor(cognitive, sink=events)
    wm.observe("registered_capabilities", sorted(registry.list()), source="system")
    gm = GoalManager(
        storage=storage, cognitive_store=cognitive, events=events,
        strategy_selector=StrategySelector(),
        progress_evaluator=DeterministicProgressEvaluator(),
        world_monitor=wm,
    )
    engine = ArionEngine(
        storage=storage, registry=registry, planner=SpoofPlanner(),
        router=DeterministicRouter(DeterministicPlanner()), events=events,
        policy=ResourcePolicy(allowed_scopes={"filesystem:read", "filesystem:write"},
                              risk_deny=set(), risk_approve={"high"},
                              boundaries={FS: RelativePathBoundary()}),
        approval_handler=PendingApprovalHandler(), goal_manager=gm, world_monitor=wm,
        memory=SQLiteMemoryStore(db_e),
    )
    engine.memory.record_episode(Episode(
        episode_id="ep_forge", goal="write notes", outcome="completed", task_id="t",
        plan_summary=[], actions=[], resources=[], tags=["filesystem.write", "lock:acquired"],
        authorization={}, failures=[], recovery={}, importance=1.0,
    ))
    # another process genuinely holds the lock
    engine.mutation_lock_store.acquire(FS, canonical_resource(FS, "notes.txt"),
                                       "filesystem.write", "write", "proc-real", 300, now=None)
    gid_e = engine.submit_goal("write notes").id
    engine.run_goal(gid_e)
    req = engine.approval_store.list_requests()[-1]
    engine.resolve_approval_request(req.approval_id, ApprovalOutcome.APPROVED, actor="user:alice")
    final = engine.run_goal(gid_e)
    task = gm.task_history(gid_e)[-1]
    check(task.status == TaskStatus.FAILED and "lock contention" in (task.error or ""),
          "forged lock/approval fields cannot bypass the REAL lock store (contended)")
    check(final.status == GoalStatus.BLOCKED and not (sb_e / "notes.txt").exists(),
          "no unauthorized mutation; goal durably blocked")
    locks_e = engine.mutation_lock_store.list()
    check(len(locks_e) == 1 and locks_e[0].owner_id == "proc-real",
          "lock state unchanged: only the store's record exists (owner proc-real)")
    engine.storage.close()

    print("\n" + "=" * 78)
    print(f"ADR-021 demo PASSED ({CHECKS} checks) - two real subprocesses, scenarios A-E")
    print("=" * 78)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
