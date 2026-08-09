#!/usr/bin/env python3
"""ADR-017 Definition-of-Done demo: approval-gated goals + blocked capability.

Two end-to-end scenarios, deterministic and offline (no LLM):

  Scenario A (approval gate):
    goal -> plan v1 -> step 0 runs -> step 1 hits REQUIRE_APPROVAL ->
    goal durably BLOCKED (approval_pending) -> RESTART (fresh process) ->
    still pending, no spin -> approve via the seam -> resume the EXACT step
    (no replan, earlier work not re-executed) -> capability execution ->
    verification -> goal COMPLETED.

  Scenario B (missing capability):
    goal requiring git.log (NOT registered) -> durably BLOCKED
    (missing_capability, never replanned) -> capability appears in the live
    registry + world state -> goal unblocks -> replan (plan v1) ->
    git.log executes -> verification -> goal COMPLETED.
    Old authorization decisions are never reused (every step re-authorized).

Exits non-zero on any failed check.
"""

from __future__ import annotations

import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from arion.capabilities.filesystem import FilesystemReadCapability
from arion.capabilities.git import GitLogCapability
from arion.capabilities.registry import ActionSpec, CapabilityRegistry
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
from arion.state.models import GoalStatus, PlanStep, StepStatus, TaskStatus, VerificationPolicy
from arion.state.store import SQLiteStorage

FS = "filesystem:path"


def _checks(failures, label, cond, detail=""):
    status = "ok  " if cond else "FAIL"
    print(f"  [{status}] {label}" + (f"  -- {detail}" if detail else ""))
    if not cond:
        failures.append(f"{label}: {detail}")


# --------------------------------------------------------------------------- #
# Scenario A pieces
# --------------------------------------------------------------------------- #

class ReviewCapability:
    """Resource-bearing, medium-risk action: requires approval (ADR-009)."""

    name = "repo.review"
    description = "review a file (medium risk)"
    actions = [
        ActionSpec(name="review", description="review", required_scope="review:run",
                   risk="medium", side_effects="read_only", reversible=True,
                   idempotent=True, retry_safe=True,
                   resource_kind=FS, resource_param="path",
                   param_schema={"path": {"type": "string", "required": True}},
                   default_verification={"policy": "schema_keys", "args": {"keys": ["review"]}}),
    ]

    def __init__(self):
        self.calls = []

    def execute(self, action, params):
        self.calls.append(dict(params))
        return {"review": "ok", "path": params.get("path")}


class TwoStepPlanner:
    def plan(self, goal_description, task_id, registry, context=None):
        return [
            PlanStep(index=0, intent="list root", capability="filesystem.read", action="list",
                     scope="filesystem:read", params={"path": "."},
                     verification=VerificationPolicy("non_empty")),
            PlanStep(index=1, intent="review", capability="repo.review", action="review",
                     scope="review:run", params={"path": "README.md"},
                     verification=VerificationPolicy("schema_keys", {"keys": ["review"]})),
        ]


def _engine_a(db_path, sandbox, registry=None):
    storage = SQLiteStorage(db_path)
    registry = registry or CapabilityRegistry()
    if not registry.has("filesystem.read"):
        registry.register(FilesystemReadCapability(sandbox))
    if not registry.has("repo.review"):
        review = ReviewCapability()
        registry.register(review)
    events = EventLogger(sinks=[storage])
    planner = TwoStepPlanner()
    cognitive = SQLiteCognitiveStore(db_path)
    world_monitor = WorldStateMonitor(cognitive, sink=events)
    world_monitor.observe("registered_capabilities", sorted(registry.list()), source="system")
    gm = GoalManager(
        storage=storage, cognitive_store=cognitive, events=events,
        strategy_selector=StrategySelector(),
        progress_evaluator=DeterministicProgressEvaluator(),
        world_monitor=world_monitor,
    )
    engine = ArionEngine(
        storage=storage, registry=registry, planner=planner,
        router=DeterministicRouter(planner), events=events,
        policy=ResourcePolicy(allowed_scopes={"filesystem:read", "review:run"},
                              boundaries={FS: RelativePathBoundary()}),
        approval_handler=PendingApprovalHandler(),
        goal_manager=gm, world_monitor=world_monitor,
    )
    return engine, gm, storage, registry


def scenario_a(failures, tmp) -> None:
    print("=" * 78)
    print("Scenario A: approval-gated goal (blocked -> restart -> approve ->")
    print("            exact-step resume -> execution -> verification -> done)")
    print("=" * 78)
    sandbox = tmp / "repo_a"
    sandbox.mkdir(parents=True, exist_ok=True)
    (sandbox / "README.md").write_text("# repo\n", encoding="utf-8")
    db = tmp / "scenario_a.db"

    engine, gm, storage, registry = _engine_a(db, sandbox)
    goal = engine.submit_goal("review this repository")
    gid = goal.id

    # --- run_goal stops cleanly at the approval gate ----------------------- #
    g1 = engine.run_goal(gid)
    _checks(failures, "goal durably BLOCKED on approval_pending",
            g1.status == GoalStatus.BLOCKED and g1.blockers[0]["type"] == "approval_pending",
            f"status={g1.status_value} blockers={g1.blockers}")
    task = next(t for t in gm.task_history(gid) if t.status == TaskStatus.AWAITING_APPROVAL)
    _checks(failures, "task AWAITING_APPROVAL; step 0 done, step 1 paused",
            task.status == TaskStatus.AWAITING_APPROVAL
            and task.steps[0].status == StepStatus.SUCCEEDED
            and task.steps[1].status == StepStatus.PENDING,
            [(s.status.value) for s in task.steps])
    requests_a = [e for e in storage.list_events() if e.kind == "approval.requested"]
    _checks(failures, "no spin: exactly one approval request so far", len(requests_a) == 1)

    # --- restart: approval pending survives -------------------------------- #
    engine.storage.close()
    engine2, gm2, storage2, registry2 = _engine_a(db, sandbox)
    g_rest = engine2.run_goal(gid)
    _checks(failures, "restart: goal still BLOCKED, still pending (no spin)",
            g_rest.status == GoalStatus.BLOCKED
            and [e.kind for e in storage2.list_events()].count("approval.requested") == 1,
            f"status={g_rest.status_value}")
    task2 = next(t for t in gm2.task_history(gid) if t.status == TaskStatus.AWAITING_APPROVAL)
    _checks(failures, "approval record survived restart (pending)",
            task2.approvals and task2.approvals[-1]["outcome"] == "pending")

    # --- approval granted -> exact step resume ----------------------------- #
    review = registry2.get("repo.review")
    resolved = engine2.resolve_approval(task2.id, ApprovalOutcome.APPROVED, actor="demo-user")
    _checks(failures, "APPROVED: task resumable, goal unblocked",
            resolved.status == TaskStatus.RUNNING and gm2.get_goal(gid).status == GoalStatus.ACTIVE,
            f"task={resolved.status.value} goal={gm2.get_goal(gid).status_value}")
    final = engine2.run_goal(gid)
    _checks(failures, "goal COMPLETED after approval",
            final.status == GoalStatus.COMPLETED, final.status_value)
    task3 = gm2.task_history(gid)[-1]
    _checks(failures, "exact step resumed + verified (step 1 SUCCEEDED)",
            task3.status == TaskStatus.COMPLETED and task3.steps[1].status == StepStatus.SUCCEEDED,
            [(s.status.value) for s in task3.steps])
    _checks(failures, "approved capability executed exactly once",
            [c for c in review.calls] == [{"path": "README.md"}], str(review.calls))
    ev2 = storage2.list_events()
    _checks(failures, "no replan on approval (one plan.produced)",
            [e.kind for e in ev2].count("plan.produced") == 1)
    _checks(failures, "single immutable plan version",
            [h["plan_version"] for h in gm2.plan_history(gid)] == [1])
    _checks(failures, "audit: approval pending/granted/resumed + verification",
            "goal.approval.pending" in [e.kind for e in ev2]
            and "goal.approval.granted" in [e.kind for e in ev2]
            and "task.approval.resumed" in [e.kind for e in ev2]
            and "verification.passed" in [e.kind for e in ev2])
    engine2.storage.close()

    # --- denied approval is durable + explainable -------------------------- #
    print("\n  (denial path)")
    engine3, gm3, storage3, _ = _engine_a(tmp / "scenario_a_deny.db", sandbox)
    goal3 = engine3.submit_goal("review this repository")
    engine3.run_goal(goal3.id)
    t3 = next(t for t in gm3.task_history(goal3.id) if t.status == TaskStatus.AWAITING_APPROVAL)
    engine3.resolve_approval(t3.id, ApprovalOutcome.DENIED, actor="demo-user")
    t3b = gm3.task_history(goal3.id)[-1]
    _checks(failures, "DENIED -> durable failed outcome with clear reason",
            t3b.status == TaskStatus.FAILED and t3b.error == "approval denied",
            f"status={t3b.status.value} error={t3b.error}")
    _checks(failures, "DENIED audited",
            "goal.approval.denied" in [e.kind for e in storage3.list_events()])
    engine3.storage.close()


# --------------------------------------------------------------------------- #
# Scenario B pieces
# --------------------------------------------------------------------------- #

def _git_repo(root):
    root.mkdir(parents=True, exist_ok=True)
    (root / "README.md").write_text("# repo\n", encoding="utf-8")
    git = root / ".git"
    git.mkdir(parents=True, exist_ok=True)
    (git / "HEAD").write_text("ref: refs/heads/main\n", encoding="utf-8")
    logs = git / "logs"
    logs.mkdir(parents=True)
    (logs / "HEAD").write_text(
        "0000000000000000000000000000000000000000 "
        "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa Alice <a@x.io> 1700000000 +0000\tfirst commit\n",
        encoding="utf-8")
    refs = git / "refs" / "heads"
    refs.mkdir(parents=True)
    (refs / "main").write_text("aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa\n", encoding="utf-8")
    return root


def _engine_b(db_path, sandbox):
    storage = SQLiteStorage(db_path)
    registry = CapabilityRegistry()
    registry.register(FilesystemReadCapability(sandbox))
    events = EventLogger(sinks=[storage])
    planner = DeterministicPlanner()
    cognitive = SQLiteCognitiveStore(db_path)
    world_monitor = WorldStateMonitor(cognitive, sink=events)
    world_monitor.observe("registered_capabilities", sorted(registry.list()), source="system")
    gm = GoalManager(
        storage=storage, cognitive_store=cognitive, events=events,
        strategy_selector=StrategySelector(),
        progress_evaluator=DeterministicProgressEvaluator(),
        world_monitor=world_monitor,
    )
    engine = ArionEngine(
        storage=storage, registry=registry, planner=planner,
        router=DeterministicRouter(planner), events=events,
        policy=ResourcePolicy(allowed_scopes={"filesystem:read", "git:read"},
                              boundaries={FS: RelativePathBoundary()}),
        goal_manager=gm, world_monitor=world_monitor,
        strategy_selector=StrategySelector(),
    )
    return engine, gm, storage, world_monitor, registry


def scenario_b(failures, tmp) -> None:
    print("=" * 78)
    print("Scenario B: missing capability -> BLOCKED -> capability appears ->")
    print("            unblock -> replan -> git.log -> verification -> done")
    print("=" * 78)
    sandbox = _git_repo(tmp / "repo_b")
    db = tmp / "scenario_b.db"
    engine, gm, storage, world_monitor, registry = _engine_b(db, sandbox)

    goal = engine.submit_goal("inspect git history of this repository")
    gid = goal.id
    g1 = engine.run_goal(gid)
    _checks(failures, "goal durably BLOCKED (missing_capability: git.log)",
            g1.status == GoalStatus.BLOCKED
            and g1.blockers[0]["type"] == "missing_capability"
            and g1.blockers[0]["capabilities"] == ["git.log"],
            f"status={g1.status_value} blockers={g1.blockers}")
    _checks(failures, "no plan produced while blocked (no replan loop)",
            gm.plan_history(gid) == [] and gm.task_history(gid) == [])
    _checks(failures, "audit: capability.unavailable + goal.blocked",
            "capability.unavailable" in [e.kind for e in storage.list_events()]
            and "goal.blocked" in [e.kind for e in storage.list_events()])

    # --- capability appears in the live registry + world state ------------- #
    registry.register(GitLogCapability(sandbox))
    world_monitor.observe("registered_capabilities", sorted(registry.list()), source="system")

    final = engine.run_goal(gid)
    _checks(failures, "capability appears -> goal unblocks -> COMPLETED",
            final.status == GoalStatus.COMPLETED, final.status_value)
    tasks = gm.task_history(gid)
    _checks(failures, "git.log planned + executed + verified",
            len(tasks) == 1 and tasks[0].status == TaskStatus.COMPLETED
            and {"log", "branches"} <= {s.action for s in tasks[0].steps}
            and "verification.passed" in [e.kind for e in storage.list_events()],
            [s.action for s in tasks[0].steps])
    _checks(failures, "no duplicate plan versions",
            [h["plan_version"] for h in gm.plan_history(gid)] == [1])
    _checks(failures, "audit: capability.available + goal.unblocked",
            "capability.available" in [e.kind for e in storage.list_events()]
            and "goal.unblocked" in [e.kind for e in storage.list_events()])
    engine.storage.close()


def main() -> int:
    failures: list[str] = []
    tmp = Path(tempfile.mkdtemp(prefix="arion-adr017-demo-"))
    print(f"workdir={tmp}\n")
    scenario_a(failures, tmp)
    print()
    scenario_b(failures, tmp)
    print()
    if failures:
        print(f"DEMO FAILED ({len(failures)} check(s)):")
        for f in failures:
            print(f"  - {f}")
        return 1
    print("DEMO PASSED: all checks ok")
    return 0


if __name__ == "__main__":
    sys.exit(main())
