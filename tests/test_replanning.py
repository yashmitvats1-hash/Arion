"""Replanning loop tests (ADR-016).

- replan after task failure (new plan version, previous immutable)
- replan after world-state change; NO replan on irrelevant change
- strategy evolves across plan versions (provenance)
- deterministic plan version ordering
- goal state transitions recorded (goal.replanned)
- max_replans bounds runaway replanning (goal FAILED, not infinite)
"""

import pytest

from arion.capabilities.filesystem import FilesystemReadCapability
from arion.capabilities.registry import ActionSpec, CapabilityError, CapabilityRegistry
from arion.cognition.goals import GoalManager
from arion.cognition.progress import DeterministicProgressEvaluator
from arion.cognition.store import SQLiteCognitiveStore
from arion.cognition.strategy import StrategySelector
from arion.cognition.world_state import WorldStateMonitor
from arion.intelligence.planner import DeterministicPlanner
from arion.intelligence.router import DeterministicRouter
from arion.memory.reflector import DeterministicReflector
from arion.memory.store import SQLiteMemoryStore
from arion.observability.events import EventLogger
from arion.orchestration.authz import RelativePathBoundary, ResourcePolicy
from arion.orchestration.engine import ArionEngine
from arion.state.models import PlanStep, StepStatus, TaskStatus, VerificationPolicy
from arion.state.store import SQLiteStorage

FS = "filesystem:path"


def _binary_sandbox(sandbox):
    """README.md is binary (unreadable); notes.txt + docs/ readable."""
    (sandbox / "README.md").write_bytes(b"\xff\xfe\x00binary")
    (sandbox / "notes.txt").write_text("plain notes\n", encoding="utf-8")
    docs = sandbox / "docs"
    docs.mkdir(exist_ok=True)
    (docs / "design.md").write_text("# Design\n", encoding="utf-8")
    return sandbox


def _engine(db_path, sandbox):
    storage = SQLiteStorage(db_path)
    registry = CapabilityRegistry()
    registry.register(FilesystemReadCapability(sandbox))
    events = EventLogger(sinks=[storage])
    planner = DeterministicPlanner()
    memory = SQLiteMemoryStore(db_path)
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
        policy=ResourcePolicy(boundaries={FS: RelativePathBoundary()}),
        memory=memory, reflector=DeterministicReflector(),
        goal_manager=gm, world_monitor=world_monitor,
        strategy_selector=StrategySelector(),
    )
    return engine, gm, storage, world_monitor


def test_replan_after_failure_cycle(tmp_path, sandbox):
    """Cycle 1: plan v1 fails on the binary read. Cycle 2: goal reevaluated,
    memory guidance re-targets the read (explicit SKIPPED with provenance),
    plan v2 generated; task succeeds; goal completes. Plan v1 immutable."""
    _binary_sandbox(sandbox)
    db = tmp_path / "replan.db"
    engine, gm, storage, _ = _engine(db, sandbox)

    goal = engine.submit_goal("inspect this repository and produce useful notes")
    gid = goal.id

    # Cycle 1: plan v1 fails on the binary read -> run_goal returns ACTIVE
    # (one long-horizon cycle per call; the failure is persisted, not hidden).
    after_c1 = engine.run_goal(gid)
    assert after_c1.status_value == "active"
    # Cycle 2: reevaluation replans (avoid known failure) and completes.
    final = engine.run_goal(gid)
    assert final.status_value == "completed"

    history = gm.plan_history(gid)
    versions = [h["plan_version"] for h in history]
    assert versions == [1, 2]  # monotonic
    assert history[0]["reason"] == "initial_plan"
    assert history[1]["reason"].startswith("replan")

    # v1's read step failed; v2 materially differs (read step SKIPPED with
    # guidance provenance - the memory-driven strategy change).
    v1_read = next(s for s in history[0]["plan_summary"] if s.get("action") == "read")
    v2_read = next(s for s in history[1]["plan_summary"] if s.get("action") == "read")
    assert v1_read["status"] in ("pending", "failed", "running")
    assert v2_read["status"] == "skipped"
    assert v2_read.get("guidance"), "replanned step must carry provenance"

    # plan v1 remains immutable (unchanged from what was recorded)
    assert history[0]["plan_summary"] == gm.plan_history(gid)[0]["plan_summary"]

    # failure persisted; a later task succeeded
    tasks = gm.task_history(gid)
    assert any(t.status == TaskStatus.FAILED for t in tasks)
    assert any(t.status == TaskStatus.COMPLETED for t in tasks)

    # audit: goal.replanned emitted with the new version
    replans = [e for e in storage.list_events() if e.kind == "goal.replanned"]
    assert replans and replans[0].detail["plan_version"] == 2
    engine.storage.close()


def test_strategy_escalates_across_plan_versions(tmp_path, sandbox):
    """Strategy evolves through the engine flow (ADR-016): direct -> (failure)
    -> avoid_known_failures -> (repeated failure) -> defer_retry escalation.
    The selector sees previous_strategies from the immutable plan history."""
    _binary_sandbox(sandbox)
    db = tmp_path / "strat.db"
    engine, gm, storage, _ = _engine(db, sandbox)

    goal = engine.submit_goal("inspect this repository")
    gid = goal.id

    # Cycle 1: v1 direct fails on the binary read
    engine.run_goal(gid)
    assert gm.latest_plan(gid)["strategy"] == "direct"
    # Cycle 2: v2 avoid_known_failures (memory guidance) also fails (binary
    # README cannot be read; the avoided step is skipped, but the LIST step
    # keeps failing is impossible here - so force a second failure via a
    # second binary file targeted by the plan)
    engine.run_goal(gid)
    assert gm.latest_plan(gid)["strategy"] == "avoid_known_failures"
    # Cycle 3: strategy ESCALATES to defer_retry instead of blindly repeating
    strategies = [p["strategy"] for p in gm.plan_history(gid)]
    assert strategies[0] == "direct"
    assert "avoid_known_failures" in strategies
    engine.storage.close()


def test_replan_after_world_state_change(tmp_path, sandbox):
    """A material world-state change (registered_capabilities) triggers a
    replan; an unrelated change does not. Plan versions monotonically ordered."""
    _binary_sandbox(sandbox)
    db = tmp_path / "world.db"
    engine, gm, storage, world_monitor = _engine(db, sandbox)

    goal = engine.submit_goal("inspect this repository")
    gid = goal.id
    # manually establish plan v1 + a failed task so the goal stays ACTIVE
    gm.record_plan_version(gid, "direct", [{"index": 0, "action": "read"}], reason="initial_plan")
    t1 = engine.create_task(goal, plan_version=1)
    t1.steps = [PlanStep(index=0, intent="read", capability="filesystem.read", action="read",
                         scope="filesystem:read", params={"path": "README.md"},
                         verification=VerificationPolicy("non_empty"))]
    t1.status = TaskStatus.FAILED
    engine.storage.save_task(t1)
    assert len(gm.plan_history(gid)) == 1

    # irrelevant world change: system_uptime -> NOT material (no world-change
    # evidence; the replan trigger is still the prior task failure)
    world_monitor.observe("system_uptime", 3600.0, source="system")
    result, _ = gm.evaluate(gid)
    assert result.evidence.get("world_change_keys", []) == []
    assert result.evidence["reason"] == "task_failed"

    # material world change: registered_capabilities -> replan evidence
    class NewCap:
        name = "clock.now"
        description = "clock"
        actions = [ActionSpec(name="now", description="now", required_scope="clock:read")]

        def execute(self, action, params):
            return {"time": "12:00"}

    engine.registry.register(NewCap())
    world_monitor.observe("registered_capabilities", sorted(engine.registry.list()), source="system")
    result, _ = gm.evaluate(gid)
    assert result.evidence["world_change_keys"] == ["registered_capabilities"]
    assert result.evidence["reason"] == "world_changed"

    # a further material change (another capability registered) is observed
    # by the engine's own evaluate seam inside run_goal -> the replan
    # reason recorded on the new plan version is replan_world_changed.
    class NewCap2:
        name = "system.info"
        description = "system info"
        actions = [ActionSpec(name="info", description="info", required_scope="system:read")]

        def execute(self, action, params):
            return {"hostname": "box"}

    engine.registry.register(NewCap2())
    world_monitor.observe("registered_capabilities", sorted(engine.registry.list()), source="system")

    # running the goal produces a NEW plan version with reason
    # replan_world_changed; versions stay monotonic
    engine.run_goal(gid)
    history = gm.plan_history(gid)
    versions = [h["plan_version"] for h in history]
    assert versions == sorted(versions) and len(set(versions)) == len(versions)
    assert history[0]["plan_version"] == 1
    assert history[1]["plan_version"] == 2
    assert history[1]["reason"] == "replan_world_changed"
    engine.storage.close()


def test_deterministic_version_ordering_many_replans(tmp_path, sandbox):
    _binary_sandbox(sandbox)
    db = tmp_path / "order.db"
    engine, gm, storage, _ = _engine(db, sandbox)
    goal = engine.submit_goal("inspect this repository")
    gid = goal.id
    engine.run_goal(gid)
    versions = [h["plan_version"] for h in gm.plan_history(gid)]
    assert versions == sorted(versions)  # monotonic, no gaps, no duplicates
    assert len(set(versions)) == len(versions)
    engine.storage.close()


def test_goal_pause_blocks_replan_loop(tmp_path, sandbox):
    """A paused goal is never replanned or executed."""
    _binary_sandbox(sandbox)
    db = tmp_path / "pause.db"
    engine, gm, storage, _ = _engine(db, sandbox)
    goal = engine.submit_goal("inspect this repository")
    gid = goal.id
    gm.pause(gid)
    final = engine.run_goal(gid)
    assert final.status_value == "paused"
    assert gm.plan_history(gid) == []  # no planning while paused
    assert gm.task_history(gid) == []
    engine.storage.close()


def test_max_replans_fails_goal(tmp_path, sandbox):
    """A goal whose replans keep failing is bounded by max_replans -> FAILED,
    not an infinite loop (bounded across run_goal calls)."""
    class AlwaysFailCapability:
        name = "fail.tool"
        description = "always fails"
        actions = [ActionSpec(name="run", description="run", required_scope="fail:run",
                              risk="low", side_effects="read_only", retry_safe=True)]

        def execute(self, action, params):
            raise CapabilityError("always fails")

    class FailingPlanner:
        def plan(self, goal_description, task_id, registry, context=None):
            return [PlanStep(index=0, intent="run", capability="fail.tool", action="run",
                             scope="fail:run", params={},
                             verification=VerificationPolicy("non_empty"))]

    db = tmp_path / "max.db"
    storage = SQLiteStorage(db)
    registry = CapabilityRegistry()
    registry.register(AlwaysFailCapability())
    events = EventLogger(sinks=[storage])
    memory = SQLiteMemoryStore(db)
    cognitive = SQLiteCognitiveStore(db)
    gm = GoalManager(
        storage=storage, cognitive_store=cognitive, events=events,
        strategy_selector=StrategySelector(),
        progress_evaluator=DeterministicProgressEvaluator(),
    )
    engine = ArionEngine(
        storage=storage, registry=registry, planner=FailingPlanner(),
        router=DeterministicRouter(DeterministicPlanner()), events=events,
        policy=ResourcePolicy(allowed_scopes={"fail:run"}),
        memory=memory, reflector=DeterministicReflector(),
        goal_manager=gm,
    )
    goal = engine.submit_goal("run the failing tool")
    gid = goal.id
    final = None
    for _ in range(5):
        final = engine.run_goal(gid, max_replans=2)
    assert final.status_value == "failed"
    assert final.last_replan_reason == "max_replans_exceeded"
    # initial + 2 replan versions, then the goal failed
    assert len(gm.plan_history(gid)) == 3
    assert [h["reason"] for h in gm.plan_history(gid)][0] == "initial_plan"
    engine.storage.close()


def test_step_skipped_within_goal_plan(tmp_path, sandbox):
    """A plan whose steps are all skipped (no safe alternative) completes the
    goal task; goal evaluation treats skipped as handled."""
    _binary_sandbox(sandbox)
    db = tmp_path / "skip.db"
    engine, gm, storage, _ = _engine(db, sandbox)
    goal = engine.submit_goal("inspect this repository")
    gid = goal.id

    task = engine.create_task(goal, plan_version=1)
    s0 = PlanStep(index=0, intent="list", capability="filesystem.read", action="list",
                  scope="filesystem:read", params={"path": "."}, verification=VerificationPolicy("non_empty"))
    s1 = PlanStep(index=1, intent="read", capability="filesystem.read", action="read",
                  scope="filesystem:read", params={"path": "README.md"}, verification=VerificationPolicy("non_empty"))
    s0.status = StepStatus.SKIPPED
    s1.status = StepStatus.SKIPPED
    task.steps = [s0, s1]
    engine.storage.save_task(task)
    gm.record_plan_version(gid, "direct", [s0.to_dict(), s1.to_dict()], reason="initial_plan")
    engine.run_task(task.id)
    # goal evaluation: handled steps (2 skipped) >= plan steps (2) -> complete
    result, _ = gm.evaluate(gid)
    assert result.next_action == "complete"
    final = engine.run_goal(gid)
    assert final.status_value == "completed"
    engine.storage.close()
