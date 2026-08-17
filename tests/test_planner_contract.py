"""Planner required-capability contract tests (ADR-018, Phase E).

- The Planner contract explicitly requires required_capabilities().
- A planner that cannot declare requirements FAILS CLOSED (the goal is
  durably BLOCKED, never planned, never executed) instead of silently
  bypassing the missing-capability gate.
- A model-proposed plan referencing an unregistered capability is rejected at
  validation and NEVER executes.
- When the capability appears later, the goal replans through the normal
  path; old plan/authorization decisions are never reused.
"""

import pytest

from arion.capabilities.filesystem import FilesystemReadCapability
from arion.capabilities.git import GitLogCapability
from arion.capabilities.registry import CapabilityRegistry
from arion.cognition.goals import GoalManager
from arion.cognition.progress import DeterministicProgressEvaluator
from arion.cognition.store import SQLiteCognitiveStore
from arion.cognition.strategy import StrategySelector
from arion.cognition.world_state import WorldStateMonitor
from arion.intelligence.planner import DeterministicPlanner, Planner
from arion.intelligence.router import DeterministicRouter
from arion.observability.events import EventLogger
from arion.orchestration.authz import RelativePathBoundary, ResourcePolicy
from arion.orchestration.engine import ArionEngine
from arion.state.models import GoalStatus, PlanStep, TaskStatus, VerificationPolicy
from arion.state.store import SQLiteStorage

FS = "filesystem:path"


class SilentPlanner:
    """A planner that does NOT implement required_capabilities (the old
    silent hasattr no-op case)."""

    def plan(self, goal_description, task_id, registry, context=None):
        raise AssertionError("planning must never be reached: gate fails closed")

    # NOTE: no required_capabilities method on purpose


def _engine(db_path, sandbox, planner, with_git=False):
    storage = SQLiteStorage(db_path)
    registry = CapabilityRegistry()
    registry.register(FilesystemReadCapability(sandbox))
    if with_git:
        registry.register(GitLogCapability(sandbox))
    events = EventLogger(sinks=[storage])
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
    return engine, gm, storage, registry


def test_planner_contract_is_explicit():
    """The Planner protocol demands required_capabilities (typed)."""
    assert hasattr(Planner, "required_capabilities")
    ann = getattr(Planner.required_capabilities, "__annotations__", {})
    assert "return" in ann  # -> set[str]
    assert hasattr(DeterministicPlanner(), "required_capabilities")
    assert hasattr(DeterministicPlanner(), "plan")


def test_planner_without_required_capabilities_fails_closed(tmp_path, sandbox):
    """A planner that cannot declare its requirements is never silently
    trusted: the goal is durably BLOCKED (planner_contract) - planning and
    execution never happen."""
    engine, gm, storage, _ = _engine(tmp_path / "fc.db", sandbox, SilentPlanner())
    goal = engine.submit_goal("inspect this repository")
    gid = goal.id
    final = engine.run_goal(gid)
    assert final.status == GoalStatus.BLOCKED
    assert final.blockers and final.blockers[0]["type"] == "planner_contract"
    assert gm.plan_history(gid) == []       # never planned
    assert gm.task_history(gid) == []       # never executed
    # no replan loop: repeated calls stay blocked
    for _ in range(3):
        assert engine.run_goal(gid).status == GoalStatus.BLOCKED
    engine.storage.close()


def test_model_proposed_unregistered_capability_never_executed(tmp_path, sandbox):
    """A model-backed plan proposing an unregistered capability is rejected at
    validation - the capability is never executed."""
    from arion.intelligence.model_planner import RealModelPlanner
    from arion.intelligence.errors import PlanCapabilityValidationError

    class EvilRouter:
        def plan_structured(self, goal_description, catalog, context=None):
            from arion.intelligence.plan_schema import PlanSchema, StructuredStep

            return PlanSchema(
                version="1.0",
                intent="run evil",
                steps=[StructuredStep(
                    intent="exec", capability="shell.exec", action="exec",
                    params={"cmd": "rm -rf /"},
                    verification={"policy": "non_empty", "args": {}},
                )],
            )

    storage = SQLiteStorage(tmp_path / "evil.db")
    registry = CapabilityRegistry()
    registry.register(FilesystemReadCapability(sandbox))
    planner = RealModelPlanner(router=EvilRouter())
    engine = ArionEngine(
        storage=storage, registry=registry, planner=planner,
        router=EvilRouter(), events=EventLogger(sinks=[storage]),
        policy=ResourcePolicy(boundaries={FS: RelativePathBoundary()}),
    )
    task = engine.execute_goal("do whatever the model wants")
    assert task.status == TaskStatus.FAILED
    assert "shell.exec" in (task.error or "")  # capability_validation failure
    # nothing was ever executed
    assert [e.kind for e in storage.list_events()].count("capability.executed") == 0
    storage.close()


def test_capability_appears_later_replans_through_normal_path(tmp_path, sandbox):
    """git.log missing -> BLOCKED; appears -> unblock -> replan -> complete.
    Old plan/authorization decisions are never reused (fresh policy checks)."""
    sandbox = _make_git_repo(sandbox)
    engine, gm, storage, registry = _engine(tmp_path / "re.db", sandbox, DeterministicPlanner())
    goal = engine.submit_goal("inspect git history of this repository")
    gid = goal.id
    engine.run_goal(gid)
    assert gm.get_goal(gid).status == GoalStatus.BLOCKED

    registry.register(GitLogCapability(sandbox))
    engine.world_monitor.observe("registered_capabilities", sorted(registry.list()), source="system")
    final = engine.run_goal(gid)
    assert final.status == GoalStatus.COMPLETED
    assert [h["plan_version"] for h in gm.plan_history(gid)] == [1]
    # every executed step passed through fresh authorization
    kinds = [e.kind for e in storage.list_events()]
    assert kinds.count("permission.checked") >= 2
    assert "verification.passed" in kinds
    engine.storage.close()


def _make_git_repo(sandbox):
    (sandbox / "README.md").write_text("# repo\n", encoding="utf-8")
    git = sandbox / ".git"
    git.mkdir(parents=True, exist_ok=True)
    (git / "HEAD").write_text("ref: refs/heads/main\n", encoding="utf-8")
    logs = git / "logs"
    logs.mkdir(parents=True)
    (logs / "HEAD").write_text(
        "0000000000000000000000000000000000000000 "
        "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa Alice <a@x.io> 1700000000 +0000\tfirst\n",
        encoding="utf-8")
    refs = git / "refs" / "heads"
    refs.mkdir(parents=True)
    (refs / "main").write_text("aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa\n", encoding="utf-8")
    return sandbox
