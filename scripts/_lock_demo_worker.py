#!/usr/bin/env python3
"""Worker for the ADR-021 two-process lock demo.

Spawned as a REAL subprocess by scripts/demo_adr021_lock_two_process.py so
the mutation locks are exercised across independent Python processes sharing
the same SQLite DB.

Modes:
  slow-write   [--hold N]        full engine pipeline with a write capability
                                 that sleeps inside execute (lock held); prints
                                 LOCKED marker + final JSON, then exits.
  attempt-write [--goal GID]     full pipeline; on lock contention prints the
                                 goal id + blocked/contended result.
  acquire-crash [--lease N]      acquire a lock via the store, print it, then
                                 exit WITHOUT releasing (crash simulation).
  reclaim-write                  reclaim stale locks, then run the full
                                 pipeline to completion.
  approve-run  [--goal GID]      approve the latest pending request, restart
                                 the engine (fresh process state), resume the
                                 goal to completion.
  fail-run     [--goal GID]      full pipeline with an always-failing write
                                 capability (mutation failure -> recovery).
  recover-run  [--goal GID]      acknowledge recovery, approve fresh request,
                                 run with a NORMAL capability to completion.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

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
from arion.observability.events import EventLogger
from arion.orchestration.authz import (
    ApprovalOutcome,
    PendingApprovalHandler,
    RelativePathBoundary,
    ResourcePolicy,
)
from arion.orchestration.engine import ArionEngine
from arion.state.locks import canonical_resource
from arion.state.models import GoalStatus, PlanStep, VerificationPolicy
from arion.state.store import SQLiteStorage

FS = "filesystem:path"


class WritePlanner:
    def plan(self, goal_description, task_id, registry, context=None):
        return [
            PlanStep(index=0, intent="write notes", capability="filesystem.write", action="write",
                     scope="filesystem:write",
                     params={"path": "notes.txt", "content": "hello", "overwrite": True},
                     verification=VerificationPolicy("write_verified")),
        ]

    def required_capabilities(self, goal_description):
        return {"filesystem.write"}


class SlowWrite(FilesystemWriteCapability):
    """Holds the mutation lock (execute runs under the engine's lock) and
    signals the parent process, then writes."""

    def __init__(self, sandbox, hold: float):
        super().__init__(sandbox)
        self.hold = hold

    def execute(self, action, params):
        print("LOCKED", flush=True)
        time.sleep(self.hold)
        return super().execute(action, dict(params))


class AlwaysFailWrite(FilesystemWriteCapability):
    def __init__(self, sandbox):
        super().__init__(sandbox)
        self.calls = 0

    def execute(self, action, params):
        self.calls += 1
        raise CapabilityError("disk full")


def build_engine(db_path, sandbox, write_cap=None):
    storage = SQLiteStorage(db_path)
    registry = CapabilityRegistry()
    registry.register(FilesystemReadCapability(sandbox))
    registry.register(write_cap or FilesystemWriteCapability(sandbox))
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
    )
    return engine, gm


def _approve_pending(engine) -> None:
    reqs = [r for r in engine.approval_store.list_requests()
            if r.status.value == "pending"]
    if reqs:
        engine.resolve_approval_request(reqs[-1].approval_id, ApprovalOutcome.APPROVED,
                                        actor="user:alice")


def _finish(engine, gid) -> dict:
    final = engine.run_goal(gid)
    tasks = engine.goal_manager.task_history(gid)
    last = tasks[-1] if tasks else None
    out = {
        "goal_id": gid,
        "goal_status": final.status.value if hasattr(final, "status") else str(final),
        "task_status": last.status.value if last else None,
        "task_error": last.error if last else None,
        "locks": [l.to_dict() for l in engine.mutation_lock_store.list()],
    }
    engine.storage.close()
    return out


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("mode")
    ap.add_argument("--db", required=True)
    ap.add_argument("--sandbox", required=True)
    ap.add_argument("--hold", type=float, default=2.0)
    ap.add_argument("--lease", type=float, default=2.0)
    ap.add_argument("--goal", default=None)
    args = ap.parse_args()
    db, sb = args.db, Path(args.sandbox)

    if args.mode == "slow-write":
        engine, gm = build_engine(db, sb, SlowWrite(sb, args.hold))
        gid = args.goal or engine.submit_goal("write notes").id
        engine.run_goal(gid)
        _approve_pending(engine)
        print(json.dumps(_finish(engine, gid)), flush=True)
        return 0

    if args.mode == "attempt-write":
        engine, gm = build_engine(db, sb)
        gid = args.goal or engine.submit_goal("write notes").id
        engine.run_goal(gid)
        _approve_pending(engine)
        out = _finish(engine, gid)
        out["contended"] = "lock contention" in (out.get("task_error") or "")
        print(json.dumps(out), flush=True)
        return 0

    if args.mode == "queue-approval":
        engine, gm = build_engine(db, sb)
        gid = engine.submit_goal("write notes").id
        out = _finish(engine, gid)  # runs to BLOCKED (approval pending), then exits
        print(json.dumps(out), flush=True)
        return 0

    if args.mode == "acquire-crash":
        engine, gm = build_engine(db, sb)
        lock = engine.mutation_lock_store.acquire(
            FS, canonical_resource(FS, "notes.txt"), "filesystem.write", "write",
            "proc-crash", lease_seconds=args.lease, now=None)
        print(json.dumps(lock.to_dict()), flush=True)
        os._exit(0)  # crash: no release, no cleanup

    if args.mode == "reclaim-write":
        engine, gm = build_engine(db, sb)
        reclaimed = engine.reclaim_stale_locks()
        gid = engine.submit_goal("write notes").id
        engine.run_goal(gid)
        _approve_pending(engine)
        out = _finish(engine, gid)
        out["reclaimed"] = reclaimed
        print(json.dumps(out), flush=True)
        return 0

    if args.mode == "approve-run":
        engine, gm = build_engine(db, sb)
        _approve_pending(engine)
        engine.storage.close()
        # restart: fresh engine process state, same DB
        engine, gm = build_engine(db, sb)
        gid = args.goal or engine.submit_goal("write notes").id
        print(json.dumps(_finish(engine, gid)), flush=True)
        return 0

    if args.mode == "fail-run":
        engine, gm = build_engine(db, sb, AlwaysFailWrite(sb))
        gid = args.goal or engine.submit_goal("write notes").id
        engine.run_goal(gid)
        _approve_pending(engine)
        final = engine.run_goal(gid)
        cap = engine.registry.get("filesystem.write")
        recoveries = [r.to_dict() for r in engine.recovery_store.list_recoveries()]
        tasks = engine.goal_manager.task_history(gid)
        last = tasks[-1] if tasks else None
        out = {
            "goal_id": gid,
            "goal_status": final.status.value if hasattr(final, "status") else str(final),
            "task_status": last.status.value if last else None,
            "task_error": last.error if last else None,
            "locks": [l.to_dict() for l in engine.mutation_lock_store.list()],
            "cap_calls": cap.calls,
            "recoveries": recoveries,
        }
        engine.storage.close()
        print(json.dumps(out), flush=True)
        return 0

    if args.mode == "recover-run":
        engine, gm = build_engine(db, sb)  # NORMAL capability now
        gid = args.goal
        engine.run_goal(gid)  # recovery gate holds
        rec = engine.recovery_store.list_recoveries()[0]
        engine.acknowledge_recovery(rec.recovery_id, actor="user:alice")
        engine.run_goal(gid)  # fresh approval queued
        _approve_pending(engine)
        out = _finish(engine, gid)
        print(json.dumps(out), flush=True)
        return 0

    print(f"unknown mode: {args.mode}")
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
