#!/usr/bin/env python3
"""Worker for ADR-025 scheduler-restart tests (crash simulation).

Spawned as a REAL subprocess by tests/test_scheduler_restart.py so the
durable scheduler/work registry + mutation locks are exercised across
independent Python processes sharing one SQLite DB.

Modes:
  crash-running --db DB --sandbox SB [--lease N]
      Full engine pipeline for a single write goal, but the write
      capability calls os._exit(1) INSIDE execute - AFTER the engine
      acquired the durable mutation lock and marked the scheduler-work row
      RUNNING, and BEFORE the mutation completes or any step state is
      persisted. Prints CRASHED on stderr.
  crash-queued --db DB --sandbox SB
      Plan a write goal, create the QUEUED scheduler-work row for its step
      (as if admitted but never run), then os._exit(1) without dispatching.
      Prints CRASHED on stderr.
"""

from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

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
from arion.orchestration.authz import PendingApprovalHandler, RelativePathBoundary, ResourcePolicy
from arion.orchestration.engine import ArionEngine
from arion.state.models import GoalStatus, PlanStep, VerificationPolicy
from arion.state.store import SQLiteStorage

FS = "filesystem:path"


def _write_step(path: str) -> PlanStep:
    return PlanStep(index=0, intent=f"write {path}", capability="filesystem.write",
                    action="write", scope="filesystem:write",
                    params={"path": path, "content": "x", "overwrite": True},
                    verification=VerificationPolicy("write_verified"), depends_on=[])


class CrashWrite(FilesystemWriteCapability):
    """Writes nothing; dies mid-execution (after lock acquisition)."""

    def execute(self, action, params):
        os._exit(1)  # noqa: PLR1722 - deliberate crash simulation


class WritePlanner:
    """One explicit write step (a.txt) - like the ADR-021 demo worker."""

    def plan(self, goal_description, task_id, registry, context=None):
        return [PlanStep(index=0, intent="write a.txt", capability="filesystem.write",
                         action="write", scope="filesystem:write",
                         params={"path": "a.txt", "content": "x", "overwrite": True},
                         verification=VerificationPolicy("write_verified"), depends_on=[])]

    def required_capabilities(self, goal_description):
        return {"filesystem.write"}


def _build(db: str, sandbox: str, lease: float):
    storage = SQLiteStorage(db)
    registry = CapabilityRegistry()
    registry.register(FilesystemReadCapability(sandbox))
    registry.register(CrashWrite(sandbox))
    events = EventLogger(sinks=[storage])
    cognitive = SQLiteCognitiveStore(db)
    wm = WorldStateMonitor(cognitive, sink=events)
    wm.observe("registered_capabilities", sorted(registry.list()), source="system")
    gm = GoalManager(
        storage=storage, cognitive_store=cognitive, events=events,
        strategy_selector=None, progress_evaluator=DeterministicProgressEvaluator(),
        world_monitor=wm,
    )
    policy = ResourcePolicy(
        allowed_scopes={"filesystem:read", "filesystem:write"},
        risk_deny=set(), risk_approve=set(),
        boundaries={FS: RelativePathBoundary()},
    )
    planner = WritePlanner()
    engine = ArionEngine(
        storage=storage, registry=registry, planner=planner,
        router=DeterministicRouter(planner), events=events,
        policy=policy, approval_handler=PendingApprovalHandler(),
        goal_manager=gm, world_monitor=wm,
        max_concurrency=1, lock_wait_max_seconds=0.0,
        mutation_lock_lease_seconds=lease, scheduler_lease_seconds=lease,
    )
    return engine, gm


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("mode", choices=["crash-running", "crash-queued"])
    parser.add_argument("--db", required=True)
    parser.add_argument("--sandbox", required=True)
    parser.add_argument("--lease", type=float, default=1.0)
    args = parser.parse_args()

    sb = Path(args.sandbox)
    sb.mkdir(parents=True, exist_ok=True)
    (sb / "a.txt").write_text("a", encoding="utf-8")

    engine, gm = _build(args.db, str(sb), args.lease)
    gid = engine.submit_goal("write a").id
    engine._plan_for_goal(gid)

    if args.mode == "crash-running":
        engine.run_goal(gid)  # CrashWrite exits the process inside execute
    elif args.mode == "crash-queued":
        task = gm.pending_task(gid)
        engine.scheduler_registry.create(
            task_id=task.id, goal_id=gid, step_index=0,
            scheduler_id=engine.scheduler_id)
        os._exit(1)  # noqa: PLR1722 - die before dispatching
    return 0


if __name__ == "__main__":
    sys.exit(main())
