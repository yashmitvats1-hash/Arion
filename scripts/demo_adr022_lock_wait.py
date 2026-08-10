#!/usr/bin/env python3
"""ADR-022 DoD demo: bounded lock-contention waiting/backoff (A-C).

Real subprocesses (A + B) share one SQLite DB; the DB is the coordination
authority.

  A  wait -> success:  A holds the mutation lock; B plans + authorizes,
     hits A's lock, enters BOUNDED WAITING (durable, backoff), A releases
     before the deadline, B retries, re-validates authorization, acquires,
     mutates EXACTLY once, verifies, releases, completes.
  B  timeout:           A holds past B's deadline -> B fails durably with a
     typed timeout, NO mutation, NO recovery record (contention != failure).
  C  stale authorization: approval -> contention -> the LIVE ActionSpec
     tightens while B waits -> lock frees -> post-wait re-validation DENIES
     the stale grant (no mutation) -> capability restored -> goal replans
     -> FRESH approval -> mutation.

Semantic constraints (proven here):
- waiting retries COORDINATION only (never the mutation, never the plan,
  never the approval);
- waiting never grants authority; the engine re-checks live authorization
  before mutating after a wait;
- memory/cognition/strategy/model output cannot modify the wait budget.
Deterministic and offline (no LLM, no shell).
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


class TightenedWrite(FilesystemWriteCapability):
    """Live ActionSpec tightened while a task is waiting (scope change)."""

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


def build_engine(db_path, sandbox, registry_holder=None):
    storage = SQLiteStorage(db_path)
    registry = CapabilityRegistry()
    registry.register(FilesystemReadCapability(sandbox))
    registry.register(FilesystemWriteCapability(sandbox))
    events = EventLogger(sinks=[storage])
    planner = WritePlanner()
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
        policy=ResourcePolicy(allowed_scopes={"filesystem:read", "filesystem:write"},
                              risk_deny=set(), risk_approve={"high"},
                              boundaries={FS: RelativePathBoundary()}),
        approval_handler=PendingApprovalHandler(), goal_manager=gm, world_monitor=wm,
        lock_wait_max_seconds=60.0, lock_wait_backoff_base=0.1,
        lock_wait_backoff_max=0.2,
    )
    if registry_holder is not None:
        registry_holder["registry"] = registry
    return engine, gm


def main() -> int:
    work = Path(tempfile.mkdtemp(prefix="arion-adr022-demo-"))
    print("=" * 78)
    print("ADR-022 demo: bounded lock-contention waiting/backoff (A-C)")
    print("=" * 78)

    # ------------------------------------------------------------ scenario A
    print("\n[A] wait -> success: B waits (bounded) on A's lock; A releases;")
    print("    B re-validates, acquires, mutates once, verifies")
    sb_a = work / "a" / "repo"
    sb_a.mkdir(parents=True)
    db_a = work / "a" / "arion.db"
    a = spawn_worker("hold-release", "--db", str(db_a), "--sandbox", str(sb_a),
                     "--hold", "1.5", "--wait-max", "60")
    assert a.stdout.readline().strip() == "HOLDING"
    b = json.loads(run_worker("wait-write", "--db", str(db_a), "--sandbox", str(sb_a),
                              "--wait-max", "20", "--backoff-base", "0.1",
                              "--backoff-max", "0.2"))
    a.wait()
    check(b["goal_status"] == "completed" and b["task_status"] == "completed",
          "process B: waited -> acquired after A released -> completed")
    check("mutation.lock.waiting" in b["lock_events"] and "mutation.lock.retry" in b["lock_events"],
          "process B: entered durable WAITING and retried with backoff")
    check(b["locks"] == [], "process B: lock released after the mutation")
    check((sb_a / "notes.txt").read_text(encoding="utf-8") == "hello",
          "exactly one mutation with the intended content")

    # ------------------------------------------------------------ scenario B
    print("\n[B] timeout: A holds past B's deadline -> durable typed timeout,")
    print("    NO mutation, NO recovery record")
    sb_b = work / "b" / "repo"
    sb_b.mkdir(parents=True)
    db_b = work / "b" / "arion.db"
    a2 = spawn_worker("hold-release", "--db", str(db_b), "--sandbox", str(sb_b),
                      "--hold", "8", "--wait-max", "60")
    assert a2.stdout.readline().strip() == "HOLDING"
    b2 = json.loads(run_worker("wait-write", "--db", str(db_b), "--sandbox", str(sb_b),
                               "--wait-max", "1.0", "--backoff-base", "0.1",
                               "--backoff-max", "0.2"))
    a2.terminate()
    check(b2["task_status"] == "failed" and "wait timed out" in (b2["task_error"] or ""),
          "process B: deadline expiry -> durable, typed timeout failure")
    check(b2["goal_status"] == "blocked", "process B: goal durably BLOCKED (explainable)")
    check("mutation.lock.timeout" in b2["lock_events"], "mutation.lock.timeout audited")
    check(not (sb_b / "notes.txt").exists(), "NO mutation on the timeout path")
    st = SQLiteStorage(db_b)
    check(st.list_recoveries() == [], "NO recovery record (lock contention != mutation failure)")
    st.close()

    # ------------------------------------------------------------ scenario C
    print("\n[C] stale authorization: approval -> contention -> LIVE ActionSpec")
    print("    tightens while waiting -> re-validation denies -> fresh path")
    sb_c = work / "c" / "repo"
    sb_c.mkdir(parents=True)
    db_c = work / "c" / "arion.db"
    holder_box = {"registry": None, "lock": None, "store": None}

    # another process holds the lock
    from arion.state.locks import canonical_resource

    holder_store = SQLiteStorage(db_c)
    holder_lock = holder_store.acquire(FS, canonical_resource(FS, "notes.txt"),
                                       "filesystem.write", "write", "proc-holder",
                                       3600.0, now=None)
    holder_box["lock"] = holder_lock
    holder_box["store"] = holder_store

    engine, gm = build_engine(db_c, sb_c, registry_holder=holder_box)
    registry = holder_box["registry"]
    gid = engine.submit_goal("write notes").id
    engine.run_goal(gid)
    req = engine.approval_store.list_requests()[-1]
    engine.resolve_approval_request(req.approval_id, ApprovalOutcome.APPROVED, actor="user:alice")

    class TightenDuringWait:
        def __init__(self):
            self.n = 0

        def sleep(self, seconds):
            self.n += 1
            if self.n == 1:
                registry.register(TightenedWrite(sb_c))  # scope tightens mid-wait
            if self.n == 2:
                holder_store.release(holder_lock.lock_id, "proc-holder")  # then the lock frees
            time.sleep(seconds)

    engine.lock_sleeper = TightenDuringWait().sleep
    final = engine.run_goal(gid)
    task = gm.task_history(gid)[-1]
    check(task.status == TaskStatus.FAILED and "not permitted" in (task.error or ""),
          "post-wait re-validation DENIED the stale grant (live ActionSpec authoritative)")
    check(not (sb_c / "notes.txt").exists(), "NO mutation under the stale authorization")
    kinds = [e.kind for e in engine.storage.list_events()]
    check("permission.denied" in kinds and "mutation.lock.released" in kinds,
          "re-validation + lock release audited")

    # restore the capability -> the goal replans and needs FRESH authorization
    registry.register(FilesystemWriteCapability(sb_c))
    final = engine.run_goal(gid)
    task = gm.task_history(gid)[-1]
    check(task.status == TaskStatus.AWAITING_APPROVAL, "goal replanned -> FRESH approval queued")
    fresh = [r for r in engine.approval_store.list_requests() if r.status.value == "pending"][-1]
    engine.resolve_approval_request(fresh.approval_id, ApprovalOutcome.APPROVED, actor="user:alice")
    final = engine.run_goal(gid)
    check(final.status == GoalStatus.COMPLETED, "fresh approval -> mutation -> completed")
    check((sb_c / "notes.txt").read_text(encoding="utf-8") == "hello",
          "mutation ran exactly once under the fresh authorization path")
    holder_store.close()
    engine.storage.close()

    print("\n" + "=" * 78)
    print(f"ADR-022 demo PASSED ({CHECKS} checks) - wait/success, timeout, stale-authz")
    print("=" * 78)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
