"""Goal lifecycle tests (ADR-016).

- explicit goal states ACTIVE/PAUSED/BLOCKED/COMPLETED/FAILED/CANCELLED
- persistent + restart-safe
- explicit auditable transitions; invalid transitions FAIL CLOSED
- goal version increments; Goal/Task compatibility preserved
"""

import pytest

from arion.capabilities.filesystem import FilesystemReadCapability
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
from arion.state.models import Goal, GoalStateError, GoalStatus
from arion.state.store import SQLiteStorage


def _build_goal_manager(db_path, storage=None):
    storage = storage or SQLiteStorage(db_path)
    cognitive = SQLiteCognitiveStore(db_path)
    return GoalManager(
        storage=storage, cognitive_store=cognitive,
        strategy_selector=StrategySelector(),
        progress_evaluator=DeterministicProgressEvaluator(),
    ), storage, cognitive


def _engine(db_path, sandbox):
    storage = SQLiteStorage(db_path)
    registry = CapabilityRegistry()
    registry.register(FilesystemReadCapability(sandbox))
    events = EventLogger(sinks=[storage])
    planner = DeterministicPlanner()
    cognitive = SQLiteCognitiveStore(db_path)
    gm = GoalManager(
        storage=storage, cognitive_store=cognitive, events=events,
        strategy_selector=StrategySelector(),
        progress_evaluator=DeterministicProgressEvaluator(),
        world_monitor=WorldStateMonitor(cognitive, sink=events),
    )
    return ArionEngine(
        storage=storage, registry=registry, planner=planner,
        router=DeterministicRouter(planner), events=events,
        policy=ResourcePolicy(boundaries={"filesystem:path": RelativePathBoundary()}),
        goal_manager=gm, memory=None,
    ), gm, storage, events


# ---------------------------------------------------------------------------
# State machine
# ---------------------------------------------------------------------------


def test_goal_states_explicit():
    assert {s.value for s in GoalStatus} == {
        "active", "paused", "blocked", "completed", "failed", "cancelled"
    }


def test_goal_created_active_with_audit(tmp_path, sandbox):
    engine, gm, storage, events = _engine(tmp_path / "a.db", sandbox)
    goal = gm.create_goal("inspect the repository")
    assert goal.status_value == "active"
    assert goal.version == 1
    kinds = [e.kind for e in storage.list_events()]
    assert "goal.created" in kinds
    created = [e for e in storage.list_events() if e.kind == "goal.created"][0]
    assert created.detail["goal_id"] == goal.id
    engine.storage.close()


def test_valid_transitions(tmp_path, sandbox):
    engine, gm, storage, events = _engine(tmp_path / "b.db", sandbox)
    goal = gm.create_goal("inspect")
    gid = goal.id

    gm.pause(gid)
    assert gm.get_goal(gid).status_value == "paused"
    assert gm.get_goal(gid).version == 2

    gm.resume(gid)
    assert gm.get_goal(gid).status_value == "active"
    assert gm.get_goal(gid).version == 3

    gm.cancel(gid)
    assert gm.get_goal(gid).status_value == "cancelled"
    # cancelled is terminal
    with pytest.raises(GoalStateError):
        gm.resume(gid)

    kinds = [e.kind for e in storage.list_events()]
    assert kinds.count("goal.state.changed") == 3
    transitions = [e for e in storage.list_events() if e.kind == "goal.state.changed"]
    assert [t.detail["to"] for t in transitions] == ["paused", "active", "cancelled"]
    engine.storage.close()


def test_invalid_transition_fails_closed(tmp_path, sandbox):
    engine, gm, storage, events = _engine(tmp_path / "c.db", sandbox)
    goal = gm.create_goal("inspect")
    gid = goal.id
    # active -> completed is valid, but then completed is terminal
    gm.complete_goal(gid)
    with pytest.raises(GoalStateError, match="invalid goal transition"):
        gm.pause(gid)
    with pytest.raises(GoalStateError, match="invalid goal transition"):
        gm.cancel(gid)
    # unknown state rejected
    with pytest.raises(GoalStateError, match="unknown goal state"):
        gm.transition(gid, "teleported", "x")
    # goal unchanged after failed transitions
    assert gm.get_goal(gid).status_value == "completed"
    assert gm.get_goal(gid).version == 2  # only the valid transition bumped it
    engine.storage.close()


def test_blocked_and_clear(tmp_path, sandbox):
    engine, gm, storage, events = _engine(tmp_path / "d.db", sandbox)
    goal = gm.create_goal("inspect")
    gid = goal.id
    gm.set_blocked(gid, {"key": "missing_capability", "detail": "filesystem.write"})
    assert gm.get_goal(gid).status_value == "blocked"
    assert gm.get_goal(gid).blockers
    # idempotent blocker
    gm.set_blocked(gid, {"key": "missing_capability", "detail": "filesystem.write"})
    assert len(gm.get_goal(gid).blockers) == 1
    gm.clear_blockers(gid)
    assert gm.get_goal(gid).status_value == "active"
    assert gm.get_goal(gid).blockers == []
    engine.storage.close()


# ---------------------------------------------------------------------------
# Persistence / restart
# ---------------------------------------------------------------------------


def test_goal_state_survives_restart(tmp_path, sandbox):
    db = tmp_path / "r.db"
    storage_a = SQLiteStorage(db)
    gm_a, _, _ = _build_goal_manager(db, storage_a)
    goal = gm_a.create_goal("inspect")
    gid = goal.id
    gm_a.pause(gid)
    storage_a.close()

    # fresh process
    storage_b = SQLiteStorage(db)
    gm_b, _, _ = _build_goal_manager(db, storage_b)
    loaded = gm_b.get_goal(gid)
    assert loaded is not None
    assert loaded.status_value == "paused"
    assert loaded.version == 2
    assert loaded.description == "inspect"
    # resume works across restart
    gm_b.resume(gid)
    assert gm_b.get_goal(gid).status_value == "active"
    storage_b.close()


def test_legacy_goal_row_parses(tmp_path):
    """Old 5-column goal rows (status 'active') parse into the lifecycle Goal.

    Simulates a legacy DB: create the old table shape first, then open the
    store (which migrates by adding the lifecycle columns), then insert a
    legacy row and read it back.
    """
    import sqlite3

    db = tmp_path / "legacy.db"
    legacy = sqlite3.connect(db)
    legacy.execute(
        "CREATE TABLE goals (id TEXT PRIMARY KEY, description TEXT NOT NULL,"
        " source TEXT NOT NULL, status TEXT NOT NULL, created_at TEXT NOT NULL)"
    )
    legacy.commit()
    legacy.close()

    storage = SQLiteStorage(db)  # migration adds lifecycle columns
    storage._conn.execute(
        "INSERT INTO goals (id, description, source, status, created_at) VALUES (?,?,?,?,?)",
        ("goal_legacy", "old goal", "cli", "active", "2020-01-01T00:00:00+00:00"),
    )
    storage._conn.commit()
    goal = storage.load_goal("goal_legacy")
    assert goal is not None
    assert goal.status_value == "active"
    assert goal.version == 1
    assert goal.blockers == []
    storage.close()


def test_goal_task_compatibility(tmp_path, sandbox):
    """Existing Goal/Task flows still work through the lifecycle goal."""
    engine, gm, storage, events = _engine(tmp_path / "e.db", sandbox)
    goal = engine.submit_goal("summarize this repository")
    task = engine.create_task(goal)
    task.steps = [
        __import__("arion.state.models", fromlist=["PlanStep"]).PlanStep(
            index=0, intent="read", capability="filesystem.read", action="read",
            scope="filesystem:read", params={"path": "README.md"},
            verification=__import__("arion.state.models", fromlist=["VerificationPolicy"]).VerificationPolicy("non_empty"))
    ]
    engine.storage.save_task(task)
    result = engine.run_task(task.id)
    assert result.status.value == "completed"
    assert gm.get_goal(goal.id).status_value == "active"  # goal not auto-completed by a task
    engine.storage.close()
