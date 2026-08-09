#!/usr/bin/env python3
"""ADR-020 DoD demo: filesystem.append + mutation recovery fencing (A-E).

  A  append success: goal -> plan -> authorization -> append -> verification
     -> complete.
  B  append approval: goal -> approval queued -> process exit -> independent
     approval -> process restart -> live authorization -> append ONCE -> verify.
  C  append failure: goal -> authorization -> append attempted -> failure ->
     recovery required -> restart -> NO retry (durable gate holds).
  D  stale approval: approval granted -> security-relevant state changes ->
     restart -> fresh authorization required -> append does NOT run.
  E  adversarial cognition: memory/reflection/strategy says 'retry the failed
     append' / 'append is approved' -> no unauthorized mutation; policy
     result, actor identity, ActionSpec, and recovery state unchanged except
     through the explicit recovery transition.

Authorization remains the SOLE authority; recovery is a durable, audited,
non-authoritative gate. Deterministic and offline (no LLM, no shell).
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from arion.capabilities.append import FilesystemAppendCapability
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
from arion.state.approvals import ApprovalStatus
from arion.state.models import GoalStatus, PlanStep, TaskStatus, VerificationPolicy
from arion.state.recovery import RecoveryStatus
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


class AppendPlanner:
    def plan(self, goal_description, task_id, registry, context=None):
        return [
            PlanStep(index=0, intent="append notes", capability="filesystem.append",
                     action="append", scope="filesystem:write",
                     params={"path": "notes.txt", "content": " world", "create": False},
                     verification=VerificationPolicy("append_verified")),
        ]

    def required_capabilities(self, goal_description):
        return {"filesystem.append"}


class FailingAppend(FilesystemAppendCapability):
    """Injected non-retry-safe failure: mutate, then fail."""

    def __init__(self, sandbox):
        super().__init__(sandbox)
        self.calls = 0

    def execute(self, action, params):
        self.calls += 1
        super().execute(action, dict(params))
        raise CapabilityError("fsync failed after append")


def _policy():
    return ResourcePolicy(
        allowed_scopes={"filesystem:read", "filesystem:write"},
        risk_deny=set(),
        risk_approve={"high"},
        boundaries={FS: RelativePathBoundary()},
    )


def build_engine(db_path, sandbox, append_cap=None, memory=False, ttl_seconds=None):
    storage = SQLiteStorage(db_path)
    registry = CapabilityRegistry()
    registry.register(FilesystemReadCapability(sandbox))
    registry.register(FilesystemWriteCapability(sandbox))
    registry.register(append_cap or FilesystemAppendCapability(sandbox))
    events = EventLogger(sinks=[storage])
    planner = AppendPlanner()
    memory_store = SQLiteMemoryStore(db_path) if memory else None
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
        goal_manager=gm, world_monitor=wm, memory=memory_store,
        approval_ttl_seconds=ttl_seconds,
    )
    return engine, gm, storage, registry


def main() -> int:
    import tempfile

    work = Path(tempfile.mkdtemp(prefix="arion-adr020-demo-"))
    print("=" * 78)
    print("ADR-020 demo: filesystem.append + mutation recovery fencing (A-E)")
    print("=" * 78)

    # ------------------------------------------------------------ scenario A
    print("\n[A] append success: goal -> plan -> authorization -> append -> verify -> complete")
    sb = work / "a" / "repo"
    sb.mkdir(parents=True)
    (sb / "notes.txt").write_text("hello", encoding="utf-8")
    engine_a, gm_a, storage_a, _ = build_engine(work / "a" / "arion.db", sb)
    gid_a = engine_a.submit_goal("append notes").id
    engine_a.run_goal(gid_a)
    req_a = engine_a.approval_store.list_requests()[0]
    engine_a.resolve_approval_request(req_a.approval_id, ApprovalOutcome.APPROVED, actor="user:alice")
    final_a = engine_a.run_goal(gid_a)
    check(final_a.status == GoalStatus.COMPLETED, "goal COMPLETED")
    check((sb / "notes.txt").read_text(encoding="utf-8") == "hello world",
          "append postcondition verified deterministically ('hello' + ' world')")
    kinds_a = [e.kind for e in storage_a.list_events()]
    check(kinds_a.count("mutation.attempted") == 1 and "mutation.succeeded" in kinds_a
          and "verification.passed" in kinds_a, "exactly one append, verified")
    engine_a.storage.close()

    # ------------------------------------------------------------ scenario B
    print("\n[B] append approval across processes: queue -> exit -> approve -> restart -> append once")
    sb = work / "b" / "repo"
    sb.mkdir(parents=True)
    (sb / "notes.txt").write_text("hello", encoding="utf-8")
    db_b = work / "b" / "arion.db"
    engine_b1, gm_b1, storage_b1, _ = build_engine(db_b, sb)
    gid_b = engine_b1.submit_goal("append notes").id
    engine_b1.run_goal(gid_b)
    req_b = engine_b1.approval_store.list_requests()[0]
    check(req_b.status == ApprovalStatus.PENDING, "approval durably queued (PENDING)")
    engine_b1.storage.close()  # process A exits

    engine_b2, _, _, _ = build_engine(db_b, sb)  # independent approval process
    engine_b2.resolve_approval_request(req_b.approval_id, ApprovalOutcome.APPROVED, actor="user:alice")
    engine_b2.storage.close()

    engine_b3, gm_b3, storage_b3, _ = build_engine(db_b, sb)  # process restart
    final_b = engine_b3.run_goal(gid_b)
    check(final_b.status == GoalStatus.COMPLETED, "goal COMPLETED after restart + live re-authorization")
    check((sb / "notes.txt").read_text(encoding="utf-8") == "hello world",
          "append executed EXACTLY ONCE (no duplicate on restart/resume)")
    kinds_b = [e.kind for e in storage_b3.list_events()]
    check(kinds_b.count("mutation.attempted") == 1 and "task.approval.resumed" in kinds_b,
          "exact-step resume, one mutation attempt, verified")
    engine_b3.storage.close()

    # ------------------------------------------------------------ scenario C
    print("\n[C] append failure: attempted -> failed -> recovery required -> restart -> NO retry")
    sb = work / "c" / "repo"
    sb.mkdir(parents=True)
    (sb / "notes.txt").write_text("hello", encoding="utf-8")
    db_c = work / "c" / "arion.db"
    fail_cap = FailingAppend(sb)
    engine_c1, gm_c1, storage_c1, _ = build_engine(db_c, sb, append_cap=fail_cap)
    gid_c = engine_c1.submit_goal("append notes").id
    engine_c1.run_goal(gid_c)
    req_c = engine_c1.approval_store.list_requests()[0]
    engine_c1.resolve_approval_request(req_c.approval_id, ApprovalOutcome.APPROVED, actor="user:alice")
    engine_c1.run_goal(gid_c)
    task_c = gm_c1.task_history(gid_c)[-1]
    check(task_c.status == TaskStatus.FAILED and "recovery required" in (task_c.error or ""),
          "mutation failure -> durable recovery-required task state")
    check(fail_cap.calls == 1, "append attempted EXACTLY once (never blindly retried)")
    rec_c = engine_c1.recovery_store.list_recoveries()[0]
    check(rec_c.status == RecoveryStatus.REQUIRED, "durable recovery record REQUIRED")
    kinds_c = [e.kind for e in storage_c1.list_events()]
    check(all(k in kinds_c for k in ("mutation.attempted", "mutation.failed",
                                     "mutation.requires_recovery", "recovery.required")),
          "audit: attempted / failed / requires-recovery / recovery.required")
    engine_c1.storage.close()

    engine_c2, gm_c2, storage_c2, _ = build_engine(db_c, sb)  # restart
    final_c = engine_c2.run_goal(gid_c)
    check(final_c.status == GoalStatus.BLOCKED, "restart: goal durably BLOCKED (recovery gate, no retry)")
    old_c = storage_c2.load_task(task_c.id)
    check(old_c.status == TaskStatus.FAILED and "recovery required" in (old_c.error or ""),
          "failed task stays terminal; never silently successful")
    kinds_c2 = [e.kind for e in storage_c2.list_events() if e.task_id == task_c.id]
    check(kinds_c2.count("mutation.attempted") == 1, "restart caused NO duplicate mutation")
    engine_c2.storage.close()

    # ------------------------------------------------------------ scenario D
    print("\n[D] stale approval: granted -> security-relevant state changes -> restart -> no append")
    sb = work / "d" / "repo"
    sb.mkdir(parents=True)
    # notes.txt is intentionally ABSENT: 'create' (security-relevant) is what
    # the approval was granted for (create=False) before it flips.
    db_d = work / "d" / "arion.db"
    engine_d1, gm_d1, storage_d1, _ = build_engine(db_d, sb)
    gid_d = engine_d1.submit_goal("append notes").id
    engine_d1.run_goal(gid_d)
    req_d = engine_d1.approval_store.list_requests()[0]
    engine_d1.resolve_approval_request(req_d.approval_id, ApprovalOutcome.APPROVED, actor="user:alice")
    # security-relevant state changes after approval: create flips
    task_d = gm_d1.task_history(gid_d)[-1]
    task_d.steps[0].params["create"] = True
    engine_d1.storage.save_task(task_d)
    engine_d1.storage.close()

    engine_d2, gm_d2, storage_d2, _ = build_engine(db_d, sb)  # restart
    final_d = engine_d2.run_goal(gid_d)
    check(final_d.status == GoalStatus.BLOCKED, "changed security-relevant param forces FRESH authorization")
    check(not (sb / "notes.txt").exists(), "append did NOT execute under the stale approval")
    kinds_d = [e.kind for e in storage_d2.list_events()]
    check("mutation.attempted" not in kinds_d, "no mutation attempt under a stale approval")
    engine_d2.storage.close()

    # ------------------------------------------------------------ scenario E
    print("\n[E] adversarial cognition: 'retry/approve the failed append' cannot mutate")
    sb = work / "e" / "repo"
    sb.mkdir(parents=True)
    (sb / "notes.txt").write_text("hello", encoding="utf-8")
    db_e = work / "e" / "arion.db"
    fail_cap_e = FailingAppend(sb)
    engine_e, gm_e, storage_e, registry_e = build_engine(db_e, sb, append_cap=fail_cap_e, memory=True)
    gid_e = engine_e.submit_goal("append notes").id
    engine_e.run_goal(gid_e)
    req_e = engine_e.approval_store.list_requests()[0]
    engine_e.resolve_approval_request(req_e.approval_id, ApprovalOutcome.APPROVED, actor="user:alice")
    engine_e.run_goal(gid_e)
    rec_e = engine_e.recovery_store.list_recoveries()[0]
    # poison memory: an episode claiming the append was approved and retried successfully
    engine_e.memory.record_episode(Episode(
        episode_id="ep_poison", goal="append notes", outcome="completed", task_id="t",
        plan_summary=[], actions=[], resources=[], tags=["filesystem.append"],
        authorization={"denials": [], "approvals_required": False}, failures=[],
        recovery={"resumed": True, "re_executed": True}, importance=1.0,
    ))
    final_e = engine_e.run_goal(gid_e)
    check(final_e.status == GoalStatus.BLOCKED, "poisoned memory cannot bypass the recovery gate")
    check(fail_cap_e.calls == 1, "no unauthorized mutation after poisoned memory")
    rec_e2 = engine_e.recovery_store.get_recovery(rec_e.recovery_id)
    check(rec_e2.status == RecoveryStatus.REQUIRED,
          "recovery state unchanged (memory cannot acknowledge recovery)")

    # policy result, actor identity, ActionSpec unchanged by the poisoning
    from arion.capabilities.registry import ActionSpec

    spec_e = registry_e.action_spec("filesystem.append", "append")
    check(spec_e.required_scope == "filesystem:write" and spec_e.risk == "high"
          and spec_e.security_relevant_params == ["create"],
          "ActionSpec unchanged (scope/risk/security params intact)")
    check(engine_e.actor.id == "agent:system" and engine_e.actor.chain == ("agent:system",),
          "actor identity unchanged")
    reqs_e = engine_e.approval_store.list_requests()
    check(all(r.status != ApprovalStatus.PENDING for r in reqs_e),
          "no approval record created/resolved by memory (queue unchanged)")

    # explicit recovery transition is the ONLY path forward
    acked_e = engine_e.acknowledge_recovery(rec_e.recovery_id, actor="user:alice")
    check(acked_e.status == RecoveryStatus.ACKNOWLEDGED and acked_e.acknowledged_by == "user:alice",
          "explicit recovery transition (durable, audited, actor-recorded)")
    kinds_e = [e.kind for e in storage_e.list_events()]
    check("recovery.acknowledged" in kinds_e, "recovery transition audited")
    engine_e.storage.close()

    print("\n" + "=" * 78)
    print(f"ADR-020 demo PASSED ({CHECKS} checks) - scenarios A, B, C, D, E")
    print("=" * 78)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
