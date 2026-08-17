#!/usr/bin/env python3
"""ADR-019 DoD demo: filesystem.write under the hardened authorization/approval
architecture - scenarios A-E.

  A  approved mutation: goal -> write planned -> authorization requires
     approval -> durable queue -> (process exits) -> independent approval ->
     (process restarts) -> live re-authorization -> EXACTLY ONE write ->
     verification -> completed.
  B  denied mutation: queued -> denied -> durable failure -> file unchanged.
  C  stale approval: queued -> granted -> security-relevant request changes
     (overwrite flips) -> re-authorization denies -> file unchanged.
  D  non-retry-safe failure: approval -> mutation attempted -> mutation
     fails -> recovery-required -> restart -> no duplicate mutation.
  E  expiry: queued -> time advances -> EXPIRED -> resolution rejected ->
     durable EXPIRED audit state; nothing was written.

The capability never decides authorization; the policy + approval queue do.
Deterministic and offline (no LLM, no shell).
"""

from __future__ import annotations

import os
import sys
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
from arion.state.approvals import ApprovalError, ApprovalStatus
from arion.state.models import (
    GoalStatus,
    PlanStep,
    TaskStatus,
    VerificationPolicy,
)
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


class WritePlanner:
    """Typed planner: write one note file (planner support only through the
    existing typed planner contract - it can never authorize anything)."""

    def plan(self, goal_description, task_id, registry, context=None):
        return [
            PlanStep(index=0, intent="write notes", capability="filesystem.write",
                     action="write", scope="filesystem:write",
                     params={"path": "notes.txt", "content": "hello", "overwrite": False},
                     verification=VerificationPolicy("write_verified")),
        ]

    def required_capabilities(self, goal_description):
        return {"filesystem.write"}


class FailingWrite(FilesystemWriteCapability):
    """Injected mutation failure AFTER the write (non-retry-safe partial)."""

    def __init__(self, sandbox):
        super().__init__(sandbox)
        self.calls = 0

    def execute(self, action, params):
        self.calls += 1
        super().execute(action, dict(params))  # the mutation happens
        raise CapabilityError("fsync failed after write")


def _policy():
    return ResourcePolicy(
        allowed_scopes={"filesystem:read", "filesystem:write"},
        risk_deny=set(),
        risk_approve={"high"},  # high-risk writes REQUIRE approval
        boundaries={FS: RelativePathBoundary()},
    )


def build_engine(db_path, sandbox_root, write_cap=None, ttl_seconds=None):
    storage = SQLiteStorage(db_path)
    registry = CapabilityRegistry()
    registry.register(FilesystemReadCapability(sandbox_root))
    registry.register(write_cap or FilesystemWriteCapability(sandbox_root))
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
        policy=_policy(), approval_handler=PendingApprovalHandler(),
        goal_manager=gm, world_monitor=wm, approval_ttl_seconds=ttl_seconds,
    )
    return engine, gm, storage, registry


def sandbox_for(work: Path) -> Path:
    sb = work / "repo"
    sb.mkdir(parents=True, exist_ok=True)
    return sb


def queue_request(engine, sb, goal_text="write notes"):
    goal = engine.submit_goal(goal_text)
    engine.run_goal(goal.id)
    reqs = engine.approval_store.list_requests()
    assert len(reqs) == 1
    return engine.goal_manager.get_goal(goal.id), reqs[0]


def main() -> int:
    import tempfile

    work = Path(tempfile.mkdtemp(prefix="arion-adr019-demo-"))
    print("=" * 78)
    print("ADR-019 demo: filesystem.write + approval queue + expiry (A-E)")
    print("=" * 78)

    # ------------------------------------------------------------ scenario A
    print("\n[A] approved mutation: durable queue -> independent approval ->")
    print("    restart -> live re-authorization -> exactly one write -> verified")
    sb = sandbox_for(work / "a")
    db = work / "a" / "arion.db"
    engine_a, gm_a, storage_a, registry_a = build_engine(db, sb)
    goal_a, req_a = queue_request(engine_a, sb)
    check(goal_a.status == GoalStatus.BLOCKED, "goal durably BLOCKED awaiting approval")
    check(req_a.status == ApprovalStatus.PENDING and req_a.capability == "filesystem.write"
          and req_a.risk == "high" and req_a.side_effects == "mutating",
          "exactly one durable high-risk mutating ApprovalRequest queued")
    engine_a.storage.close()  # process exits with the request pending

    # independent approval process against the same DB
    engine_b, gm_b, storage_b, _ = build_engine(db, sb)
    engine_b.resolve_approval_request(req_a.approval_id, ApprovalOutcome.APPROVED, actor="user:alice")
    check(engine_b.approval_store.get_request(req_a.approval_id).status == ApprovalStatus.APPROVED,
          "approval resolved independently (durable record)")
    engine_b.storage.close()

    # process restarts; the exact step resumes under LIVE re-authorization
    engine_c, gm_c, storage_c, _ = build_engine(db, sb)
    final = engine_c.run_goal(goal_a.id)
    check(final.status == GoalStatus.COMPLETED, "goal COMPLETED after restart + resume")
    check((sb / "notes.txt").read_text(encoding="utf-8") == "hello",
          "exactly one file write with the intended content")
    kinds = [e.kind for e in storage_c.list_events()]
    check(kinds.count("mutation.attempted") == 1 and "mutation.succeeded" in kinds,
          "exactly ONE mutation.attempted + mutation.succeeded")
    check("task.approval.resumed" in kinds and "verification.passed" in kinds,
          "exact-step resume + write_verified postcondition confirmed")
    engine_c.storage.close()

    # ------------------------------------------------------------ scenario B
    print("\n[B] denied mutation: queued -> denied -> durable failure, file unchanged")
    sb = sandbox_for(work / "b")
    db = work / "b" / "arion.db"
    engine_b1, gm_b1, storage_b1, _ = build_engine(db, sb)
    goal_b, req_b = queue_request(engine_b1, sb)
    engine_b1.resolve_approval_request(req_b.approval_id, ApprovalOutcome.DENIED, actor="user:alice")
    task_b = gm_b1.task_history(goal_b.id)[-1]
    check(task_b.status == TaskStatus.FAILED and task_b.error == "approval denied",
          "durable denial: task FAILED with 'approval denied'")
    check(not (sb / "notes.txt").exists(), "file unchanged (never written)")
    kinds_b = [e.kind for e in storage_b1.list_events()]
    check(kinds_b.count("mutation.attempted") == 0, "no mutation was ever attempted")
    try:
        engine_b1.resolve_approval_request(req_b.approval_id, ApprovalOutcome.APPROVED)
        check(False, "denied approval cannot be re-resolved as approved")
    except ApprovalError:
        check(True, "denied approval cannot be re-resolved as approved")
    engine_b1.storage.close()

    # ------------------------------------------------------------ scenario C
    print("\n[C] stale approval: granted -> security-relevant param changes -> DENIED")
    sb = sandbox_for(work / "c")
    (sb / "notes.txt").write_text("original", encoding="utf-8")
    db = work / "c" / "arion.db"
    engine_c1, gm_c1, storage_c1, _ = build_engine(db, sb)
    goal_c, req_c = queue_request(engine_c1, sb)
    engine_c1.resolve_approval_request(req_c.approval_id, ApprovalOutcome.APPROVED, actor="user:alice")
    # the request changes: overwrite flips (security-relevant, fingerprinted)
    task_c = gm_c1.task_history(goal_c.id)[-1]
    task_c.steps[0].params["overwrite"] = True
    engine_c1.storage.save_task(task_c)
    final_c = engine_c1.run_goal(goal_c.id)
    check(final_c.status == GoalStatus.BLOCKED, "changed security-relevant param forces FRESH approval")
    check((sb / "notes.txt").read_text(encoding="utf-8") == "original",
          "file unchanged - stale approval never authorizes a mutation")
    engine_c1.storage.close()

    # ------------------------------------------------------------ scenario D
    print("\n[D] non-retry-safe failure: attempted -> failed -> recovery-required,")
    print("    restart -> NO duplicate mutation")
    sb = sandbox_for(work / "d")
    db = work / "d" / "arion.db"
    failing = FailingWrite(sb)
    engine_d1, gm_d1, storage_d1, _ = build_engine(db, sb, write_cap=failing)
    goal_d, req_d = queue_request(engine_d1, sb)
    engine_d1.resolve_approval_request(req_d.approval_id, ApprovalOutcome.APPROVED, actor="user:alice")
    engine_d1.run_goal(goal_d.id)
    task_d = gm_d1.task_history(goal_d.id)[-1]
    check(task_d.status == TaskStatus.FAILED and "recovery required" in (task_d.error or ""),
          "failed mutation enters durable recovery-required state (explainable)")
    check(failing.calls == 1, "mutation attempted EXACTLY once - never blindly retried")
    check((sb / "notes.txt").read_text(encoding="utf-8") == "hello",
          "partial write observable (mutation had happened before the failure)")
    kinds_d = [e.kind for e in storage_d1.list_events()]
    check(all(k in kinds_d for k in ("mutation.attempted", "mutation.failed", "mutation.requires_recovery")),
          "audit: attempted / failed / requires-recovery all recorded")
    engine_d1.storage.close()

    engine_d2, gm_d2, storage_d2, _ = build_engine(db, sb)
    old = storage_d2.load_task(task_d.id)
    check(old.status == TaskStatus.FAILED, "restart: failed task is terminal (never re-run)")
    final_d = engine_d2.run_goal(goal_d.id)
    check(final_d.status == GoalStatus.BLOCKED, "recovery requires a NEW plan + FRESH authorization")
    kinds_d2 = [e.kind for e in storage_d2.list_events() if e.task_id == task_d.id]
    check(kinds_d2.count("mutation.attempted") == 1, "restart caused NO duplicate mutation")
    engine_d2.storage.close()

    # ------------------------------------------------------------ scenario E
    print("\n[E] expiry: queued -> time advances -> EXPIRED -> rejected, audited")
    sb = sandbox_for(work / "e")
    db = work / "e" / "arion.db"
    engine_e, gm_e, storage_e, _ = build_engine(db, sb, ttl_seconds=60)
    goal_e, req_e = queue_request(engine_e, sb)
    engine_e.expire_stale_approvals(now="2099-01-01T00:00:00+00:00")
    req_e2 = engine_e.approval_store.get_request(req_e.approval_id)
    check(req_e2.status == ApprovalStatus.EXPIRED and req_e2.expired_at is not None,
          "stale PENDING request durably EXPIRED with timestamp")
    try:
        engine_e.resolve_approval_request(req_e.approval_id, ApprovalOutcome.APPROVED)
        check(False, "expired approval cannot be approved")
    except ApprovalError as exc:
        check("expired" in str(exc), f"expired approval cannot be approved: {exc}")
    task_e = gm_e.task_history(goal_e.id)[-1]
    check(task_e.status == TaskStatus.FAILED and "approval expired" in (task_e.error or ""),
          "awaiting task fails durably with 'approval expired' (no mutation)")
    kinds_e = [e.kind for e in storage_e.list_events()]
    check("approval.expired" in kinds_e, "approval.expired audit event recorded (retained, not pruned)")
    check(engine_e.approval_store.get_request(req_e.approval_id) is not None,
          "EXPIRED request remains auditable in the queue")
    check(not (sb / "notes.txt").exists(), "nothing was written in scenario E")
    engine_e.storage.close()

    print("\n" + "=" * 78)
    print(f"ADR-019 demo PASSED ({CHECKS} checks) - scenarios A, B, C, D, E")
    print("=" * 78)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
