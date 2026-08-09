#!/usr/bin/env python3
"""ADR-018 Definition-of-Done demo: durable approval queue + http.get.

Scenario 1 (approval queue DoD):
    Goal -> planner -> capability validation -> authorization
    -> persistent approval queue -> PROCESS EXITS
    -> INDEPENDENT CLI APPROVAL (`arion approvals approve`, real subprocess)
    -> process restarts (fresh engine, same DB)
    -> live re-authorization -> exact pending step resumes (no replan)
    -> verification -> completion

Scenario 2 (http.get DoD):
    Goal -> http.get -> PlanValidator -> url ResourceBoundary
    -> authorization -> injected HTTP transport -> verification -> completion
  plus the adversarial redirect escaping the allowed origin being DENIED
  (the escaped target is never fetched).

Deterministic and offline (no external network; fake transport injected).
Exits non-zero on any failed check.
"""

from __future__ import annotations

import subprocess
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from arion.capabilities.filesystem import FilesystemReadCapability
from arion.capabilities.http import FakeTransport, HttpGetCapability, HttpResponse, UrlBoundary
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
# Scenario 1: approval queue
# --------------------------------------------------------------------------- #

class ReviewCapability:
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

    def required_capabilities(self, goal_description):
        return {"filesystem.read", "repo.review"}


def _build_approval_engine(db_path, sandbox):
    storage = SQLiteStorage(db_path)
    registry = CapabilityRegistry()
    registry.register(FilesystemReadCapability(sandbox))
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
    return engine, gm, review


def scenario_approval_queue(failures, tmp) -> None:
    print("=" * 78)
    print("Scenario 1: approval queue DoD")
    print("  goal -> plan -> authz -> persistent queue -> process exit ->")
    print("  independent CLI approval -> restart -> exact-step resume -> done")
    print("=" * 78)
    sandbox = tmp / "repo"
    sandbox.mkdir(parents=True, exist_ok=True)
    (sandbox / "README.md").write_text("# repo\n", encoding="utf-8")
    db = tmp / "queue.db"

    # ---- process A: run until approval pending, then EXIT ------------------ #
    engine_a, gm_a, review_a = _build_approval_engine(db, sandbox)
    goal = engine_a.submit_goal("review this repository")
    gid = goal.id
    g1 = engine_a.run_goal(gid)
    _checks(failures, "goal durably BLOCKED on approval_pending",
            g1.status == GoalStatus.BLOCKED and g1.blockers[0]["type"] == "approval_pending")
    req = engine_a.approval_store.list_requests()[0]
    _checks(failures, "exactly one durable approval request queued",
            req.status.value == "pending" and req.capability == "repo.review"
            and req.resource == "README.md" and req.fingerprint,
            f"status={req.status.value}")
    task = next(t for t in gm_a.task_history(gid) if t.status == TaskStatus.AWAITING_APPROVAL)
    _checks(failures, "task AWAITING_APPROVAL; step 0 done, step 1 paused",
            task.steps[0].status == StepStatus.SUCCEEDED and task.steps[1].status == StepStatus.PENDING)
    engine_a.storage.close()  # ---- process A exits ----
    print("  (process A exited)")

    # ---- process B: INDEPENDENT CLI approval (real subprocess) ------------- #
    cli = Path(sys.executable).parent / "arion"
    proc = subprocess.run(
        [str(cli), "--db", str(db), "approvals", "list"],
        capture_output=True, text=True, timeout=60,
    )
    _checks(failures, "CLI lists the pending approval",
            proc.returncode == 0 and req.approval_id in proc.stdout, proc.stdout[-200:])
    proc = subprocess.run(
        [str(cli), "--db", str(db), "approvals", "approve", req.approval_id, "--actor", "demo-user"],
        capture_output=True, text=True, timeout=60,
    )
    _checks(failures, "independent CLI approval resolves the queue record",
            proc.returncode == 0 and "approved" in proc.stdout.lower(), proc.stdout[-200:])
    print("  (process B exited)")

    # ---- process C: restart, live re-authorization, exact-step resume ----- #
    engine_c, gm_c, review_c = _build_approval_engine(db, sandbox)
    req_c = engine_c.approval_store.get_request(req.approval_id)
    _checks(failures, "queue record durably APPROVED after restart",
            req_c.status.value == "approved" and req_c.decision_actor == "demo-user")
    final = engine_c.run_goal(gid)
    _checks(failures, "goal COMPLETED after restart + approval",
            final.status == GoalStatus.COMPLETED, final.status_value)
    task_c = gm_c.task_history(gid)[-1]
    _checks(failures, "exact pending step resumed + verified (step 1 SUCCEEDED)",
            task_c.status == TaskStatus.COMPLETED and task_c.steps[1].status == StepStatus.SUCCEEDED,
            [(s.status.value) for s in task_c.steps])
    _checks(failures, "approved capability executed exactly once",
            [c for c in review_c.calls] == [{"path": "README.md"}], str(review_c.calls))
    kinds = [e.kind for e in engine_c.storage.list_events()]
    _checks(failures, "no replan on approval (one plan version)",
            [h["plan_version"] for h in gm_c.plan_history(gid)] == [1])
    _checks(failures, "audit: approval.queued/granted + task.approval.resumed + verification",
            "approval.queued" in kinds and "approval.granted" in kinds
            and "task.approval.resumed" in kinds and "verification.passed" in kinds)
    engine_c.storage.close()


# --------------------------------------------------------------------------- #
# Scenario 2: http.get DoD
# --------------------------------------------------------------------------- #

def _build_http_engine(db_path, sandbox, transport, allowed_origins):
    storage = SQLiteStorage(db_path)
    registry = CapabilityRegistry()
    registry.register(FilesystemReadCapability(sandbox))
    registry.register(HttpGetCapability(transport=transport, allowed_origins=allowed_origins))
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
        policy=ResourcePolicy(
            allowed_scopes={"filesystem:read", "http:get"},
            boundaries={FS: RelativePathBoundary(),
                        "url": UrlBoundary(allowed_origins)},
        ),
        goal_manager=gm, world_monitor=world_monitor,
        strategy_selector=StrategySelector(),
    )
    return engine, gm, registry


def scenario_http(failures, tmp) -> None:
    print("=" * 78)
    print("Scenario 2: http.get DoD")
    print("  goal -> http.get -> PlanValidator -> url ResourceBoundary ->")
    print("  authorization -> injected transport -> verification -> done")
    print("=" * 78)
    sandbox = tmp / "repo"
    sandbox.mkdir(parents=True, exist_ok=True)
    db = tmp / "http.db"

    allowed = {"https://allowed.example.com"}
    transport = FakeTransport(routes={
        "https://allowed.example.com/data.json": HttpResponse(
            status=200, headers={"content-type": "application/json"}, body='{"ok": true}'),
        "https://allowed.example.com/start": HttpResponse(
            status=302, headers={"location": "https://evil.example.com/payload"}, body=""),
        "https://evil.example.com/payload": HttpResponse(status=200, headers={}, body="pwned"),
    })
    engine, gm, registry = _build_http_engine(db, sandbox, transport, allowed)

    # ---- happy path ------------------------------------------------------- #
    goal = engine.submit_goal("fetch https://allowed.example.com/data.json")
    final = engine.run_goal(goal.id)
    _checks(failures, "http.get goal COMPLETED",
            final.status == GoalStatus.COMPLETED, final.status_value)
    task = gm.task_history(goal.id)[-1]
    _checks(failures, "fetched + verified through the normal path",
            task.steps[0].result["body"] == '{"ok": true}'
            and "verification.passed" in [e.kind for e in engine.storage.list_events()])
    _checks(failures, "PlanValidator accepted the url resource (param_schema)",
            task.steps[0].params == {"url": "https://allowed.example.com/data.json"})

    # ---- adversarial: redirect escaping the allowed origin is DENIED ------ #
    goal2 = engine.submit_goal("fetch https://allowed.example.com/start")
    final2 = engine.run_goal(goal2.id)
    _checks(failures, "redirect escape DENIED (goal ACTIVE, task FAILED)",
            final2.status == GoalStatus.ACTIVE
            and gm.task_history(goal2.id)[-1].status == TaskStatus.FAILED,
            f"status={final2.status_value}")
    err = gm.task_history(goal2.id)[-1].error or ""
    _checks(failures, "clear reason: redirect escaped allowed origin",
            "redirect escaped" in err, err)
    _checks(failures, "the escaped target was NEVER fetched",
            "https://evil.example.com/payload" not in transport.calls, str(transport.calls))
    engine.storage.close()


def main() -> int:
    failures: list[str] = []
    tmp = Path(tempfile.mkdtemp(prefix="arion-adr018-demo-"))
    print(f"workdir={tmp}\n")
    scenario_approval_queue(failures, tmp)
    print()
    scenario_http(failures, tmp)
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
