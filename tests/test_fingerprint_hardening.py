"""Authorization fingerprint hardening tests (ADR-018, Phase D).

The canonical fingerprint covers: capability, action, required scope, risk,
side effects, resource kind, resource, and the SECURITY-RELEVANT params
declared by the ActionSpec. Changing a security-relevant parameter after an
approval forces fresh authorization; operational parameters are NOT
fingerprinted unless declared.
"""

import pytest

from arion.capabilities.filesystem import FilesystemReadCapability
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


class TransformCapability:
    """Medium-risk action with an explicit security-relevant param set."""

    name = "repo.transform"
    description = "transform a file (medium risk)"
    actions = [
        ActionSpec(name="transform", description="transform", required_scope="transform:run",
                   risk="medium", side_effects="read_only", reversible=True,
                   idempotent=True, retry_safe=True,
                   resource_kind=FS, resource_param="path",
                   security_relevant_params=["target", "mode"],
                   param_schema={"path": {"type": "string", "required": True},
                                 "target": {"type": "string", "required": True},
                                 "mode": {"type": "string", "required": False}},
                   default_verification={"policy": "schema_keys", "args": {"keys": ["output"]}}),
    ]

    def __init__(self):
        self.calls = []

    def execute(self, action, params):
        self.calls.append(dict(params))
        return {"output": "ok", "path": params.get("path")}


class TransformPlanner:
    def plan(self, goal_description, task_id, registry, context=None):
        return [
            PlanStep(index=0, intent="transform", capability="repo.transform", action="transform",
                     scope="transform:run",
                     params={"path": "README.md", "target": "out.txt", "mode": "full"},
                     verification=VerificationPolicy("schema_keys", {"keys": ["output"]})),
        ]

    def required_capabilities(self, goal_description):
        return {"repo.transform"}


def _engine(db_path, sandbox):
    storage = SQLiteStorage(db_path)
    registry = CapabilityRegistry()
    registry.register(FilesystemReadCapability(sandbox))
    registry.register(TransformCapability())
    events = EventLogger(sinks=[storage])
    planner = TransformPlanner()
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
        policy=ResourcePolicy(allowed_scopes={"filesystem:read", "transform:run"},
                              boundaries={FS: RelativePathBoundary()}),
        approval_handler=PendingApprovalHandler(),
        goal_manager=gm, world_monitor=world_monitor,
    )
    return engine, gm, storage, registry


def test_security_relevant_param_change_forces_fresh_authz(tmp_path, sandbox):
    db = tmp_path / "fp.db"
    engine, gm, storage, registry = _engine(db, sandbox)
    cap = registry.get("repo.transform")
    goal = engine.submit_goal("transform the repository")
    gid = goal.id
    engine.run_goal(gid)
    assert gm.get_goal(gid).status == GoalStatus.BLOCKED
    req = engine.approval_store.list_requests()[0]
    # the fingerprint carries the security-relevant params
    assert req.fingerprint["security_relevant_params"] == {"target": "out.txt", "mode": "full"}
    engine.resolve_approval_request(req.approval_id, ApprovalOutcome.APPROVED)

    # the plan's security-relevant param changes before resume
    task = gm.task_history(gid)[-1]
    task = engine.storage.load_task(task.id)
    task.steps[0].params["target"] = "other.txt"
    engine.storage.save_task(task)

    g2 = engine.run_goal(gid)
    assert g2.status == GoalStatus.BLOCKED  # fresh approval needed
    assert cap.calls == []                  # never executed under stale approval
    kinds = [e.kind for e in storage.list_events()]
    assert kinds.count("approval.requested") == 2
    engine.storage.close()


def test_operational_param_change_does_not_invalidate_approval(tmp_path, sandbox):
    """Operational params (not declared security-relevant) do NOT invalidate
    the approval - avoiding unnecessary incompatibility."""
    db = tmp_path / "op.db"
    engine, gm, storage, registry = _engine(db, sandbox)
    cap = registry.get("repo.transform")
    goal = engine.submit_goal("transform the repository")
    gid = goal.id
    engine.run_goal(gid)
    req = engine.approval_store.list_requests()[0]
    engine.resolve_approval_request(req.approval_id, ApprovalOutcome.APPROVED)

    # add an operational param (not declared) before resume
    task = gm.task_history(gid)[-1]
    task = engine.storage.load_task(task.id)
    task.steps[0].params["pretty"] = True
    engine.storage.save_task(task)

    final = engine.run_goal(gid)
    assert final.status == GoalStatus.COMPLETED
    assert len(cap.calls) == 1
    engine.storage.close()


def test_fingerprint_does_not_include_undeclared_param_values(tmp_path, sandbox):
    engine, gm, storage, _ = _engine(tmp_path / "fp2.db", sandbox)
    goal = engine.submit_goal("transform the repository")
    engine.run_goal(goal.id)
    req = engine.approval_store.list_requests()[0]
    fp = req.fingerprint
    # canonical fields only
    assert fp["capability"] == "repo.transform"
    assert fp["action"] == "transform"
    assert fp["scope"] == "transform:run"
    assert fp["risk"] == "medium"
    assert fp["side_effects"] == "read_only"
    assert fp["resource_kind"] == FS
    assert fp["resource"] == "README.md"
    assert set(fp["security_relevant_params"]) == {"target", "mode"}
    engine.storage.close()
