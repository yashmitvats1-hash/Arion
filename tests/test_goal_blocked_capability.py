"""blocked_missing_capability end-to-end tests (ADR-017).

- A goal whose required capability is unavailable becomes durably BLOCKED
  (missing_capability blocker), NOT a repeated-replan loop.
- When the capability appears in the live registry/world state, the goal
  unblocks, replans/resumes and completes.
- Survives restart; plan versions never duplicate; old authorization
  decisions are never reused.
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
from arion.intelligence.planner import DeterministicPlanner
from arion.intelligence.router import DeterministicRouter
from arion.observability.events import EventLogger
from arion.orchestration.authz import RelativePathBoundary, ResourcePolicy
from arion.orchestration.engine import ArionEngine
from arion.state.models import GoalStatus, TaskStatus
from arion.state.store import SQLiteStorage

FS = "filesystem:path"


def _git_repo(root):
    """A tiny .git layout (no shell): reflog + HEAD + branches."""
    (root / "README.md").write_text("# repo\n", encoding="utf-8")
    git = root / ".git"
    git.mkdir(parents=True, exist_ok=True)
    (git / "HEAD").write_text("ref: refs/heads/main\n", encoding="utf-8")
    logs = git / "logs"
    logs.mkdir(parents=True)
    (logs / "HEAD").write_text(
        "0000000000000000000000000000000000000000 "
        "1111111111111111111111111111111111111111 Alice <a@x.io> 1700000000 +0000\tfirst commit\n"
        "1111111111111111111111111111111111111111 "
        "2222222222222222222222222222222222222222 Bob <b@x.io> 1700000100 +0000\tsecond commit\n",
        encoding="utf-8",
    )
    refs = git / "refs" / "heads"
    refs.mkdir(parents=True)
    (refs / "main").write_text("2222222222222222222222222222222222222222\n", encoding="utf-8")
    (refs / "feature").write_text("1111111111111111111111111111111111111111\n", encoding="utf-8")
    return root


def _engine(db_path, sandbox, with_git=False):
    storage = SQLiteStorage(db_path)
    registry = CapabilityRegistry()
    registry.register(FilesystemReadCapability(sandbox))
    if with_git:
        registry.register(GitLogCapability(sandbox))
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


def test_missing_capability_blocks_goal_no_replan_loop(tmp_path, sandbox):
    _git_repo(sandbox)
    engine, gm, storage, _, _ = _engine(tmp_path / "m.db", sandbox)  # git.log NOT registered
    goal = engine.submit_goal("inspect git history of this repository")
    gid = goal.id

    g1 = engine.run_goal(gid)
    assert g1.status == GoalStatus.BLOCKED
    blockers = g1.blockers
    assert blockers and blockers[0]["type"] == "missing_capability"
    assert blockers[0]["capabilities"] == ["git.log"]
    # durably BLOCKED, NOT replanning: no plan, no tasks, no spin
    assert gm.plan_history(gid) == []
    assert gm.task_history(gid) == []
    kinds = [e.kind for e in storage.list_events()]
    assert "capability.unavailable" in kinds
    assert "goal.blocked" in kinds

    for _ in range(3):
        g = engine.run_goal(gid)
        assert g.status == GoalStatus.BLOCKED
    assert gm.plan_history(gid) == []  # still never planned
    assert [e.kind for e in storage.list_events()].count("capability.unavailable") == 1
    engine.storage.close()


def test_capability_appears_unblocks_and_completes(tmp_path, sandbox):
    _git_repo(sandbox)
    engine, gm, storage, world_monitor, registry = _engine(tmp_path / "u.db", sandbox)
    goal = engine.submit_goal("inspect git history of this repository")
    gid = goal.id
    engine.run_goal(gid)
    assert gm.get_goal(gid).status == GoalStatus.BLOCKED

    # capability appears in the live registry + world state
    registry.register(GitLogCapability(sandbox))
    world_monitor.observe("registered_capabilities", sorted(registry.list()), source="system")

    final = engine.run_goal(gid)
    assert final.status == GoalStatus.COMPLETED
    assert gm.plan_history(gid)[0]["reason"] == "initial_plan"
    assert [h["plan_version"] for h in gm.plan_history(gid)] == [1]
    tasks = gm.task_history(gid)
    assert len(tasks) == 1 and tasks[0].status == TaskStatus.COMPLETED
    # git steps executed + verified
    kinds = [e.kind for e in storage.list_events()]
    assert "capability.available" in kinds
    assert "goal.unblocked" in kinds
    assert "verification.passed" in kinds
    executed_steps = {e.step_id for e in storage.list_events() if e.kind == "capability.executed"}
    assert executed_steps == {"step_0", "step_1"}  # both git steps executed
    engine.storage.close()


def test_missing_capability_survives_restart_and_recovers(tmp_path, sandbox):
    _git_repo(sandbox)
    db = tmp_path / "rr.db"
    engine_a, gm_a, storage_a, _, _ = _engine(db, sandbox)
    goal_a = engine_a.submit_goal("inspect git history of this repository")
    gid = goal_a.id
    engine_a.run_goal(gid)
    assert gm_a.get_goal(gid).status == GoalStatus.BLOCKED
    engine_a.storage.close()

    engine_b, gm_b, storage_b, world_monitor_b, registry_b = _engine(db, sandbox)
    goal_b = gm_b.get_goal(gid)
    assert goal_b.status == GoalStatus.BLOCKED
    assert goal_b.blockers[0]["type"] == "missing_capability"
    # still no spin / no planning after restart
    engine_b.run_goal(gid)
    assert gm_b.plan_history(gid) == []

    registry_b.register(GitLogCapability(sandbox))
    world_monitor_b.observe("registered_capabilities", sorted(registry_b.list()), source="system")
    final = engine_b.run_goal(gid)
    assert final.status == GoalStatus.COMPLETED
    assert [h["plan_version"] for h in gm_b.plan_history(gid)] == [1]  # no duplicates
    engine_b.storage.close()


def test_authorization_decisions_never_reused_after_metadata_change(tmp_path, sandbox):
    """The goal was BLOCKED (nothing authorized). After unblock, every step
    still goes through fresh policy decisions against live metadata."""
    _git_repo(sandbox)
    engine, gm, storage, world_monitor, registry = _engine(tmp_path / "az.db", sandbox)
    goal = engine.submit_goal("inspect git history of this repository")
    gid = goal.id
    engine.run_goal(gid)
    assert gm.get_goal(gid).status == GoalStatus.BLOCKED

    # capabilities appear, but git:read is NOT allowed by the (tightened) policy
    registry.register(GitLogCapability(sandbox))
    world_monitor.observe("registered_capabilities", sorted(registry.list()), source="system")
    engine.policy = ResourcePolicy(allowed_scopes={"filesystem:read"},  # git:read denied
                                   boundaries={FS: RelativePathBoundary()})
    g2 = engine.run_goal(gid)
    assert g2.status == GoalStatus.ACTIVE  # replanned; task failed on authz
    tasks = gm.task_history(gid)
    assert tasks[-1].status == TaskStatus.FAILED
    assert "git:read" in (tasks[-1].error or "")
    assert "permission.denied" in [e.kind for e in storage.list_events()]
    engine.storage.close()
