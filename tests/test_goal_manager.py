"""GoalManager plan-versioning + replay-safety tests (ADR-016).

- monotonic, deterministic plan version ordering
- previous plans immutable (never mutated)
- replay-safe: no duplicate plan versions on restart/re-evaluation
- record why replanning occurred
- task history + progress per goal
"""

import pytest

from arion.capabilities.filesystem import FilesystemReadCapability
from arion.capabilities.registry import CapabilityRegistry
from arion.cognition.goals import GoalManager
from arion.cognition.progress import DeterministicProgressEvaluator
from arion.cognition.store import SQLiteCognitiveStore
from arion.cognition.strategy import StrategySelector
from arion.intelligence.planner import DeterministicPlanner
from arion.intelligence.router import DeterministicRouter
from arion.observability.events import EventLogger
from arion.orchestration.authz import RelativePathBoundary, ResourcePolicy
from arion.orchestration.engine import ArionEngine
from arion.state.models import Task, TaskStatus
from arion.state.store import SQLiteStorage


def _gm(db_path):
    storage = SQLiteStorage(db_path)
    cognitive = SQLiteCognitiveStore(db_path)
    gm = GoalManager(
        storage=storage, cognitive_store=cognitive,
        strategy_selector=StrategySelector(),
        progress_evaluator=DeterministicProgressEvaluator(),
    )
    return gm, storage, cognitive


def test_monotonic_version_ordering(tmp_path):
    gm, storage, cognitive = _gm(tmp_path / "v.db")
    goal = gm.create_goal("inspect")
    gid = goal.id

    v1 = gm.record_plan_version(gid, "direct", [{"index": 0}], reason="initial_plan")
    v2 = gm.record_plan_version(gid, "avoid_known_failures", [{"index": 0}, {"index": 1}], reason="replan_task_failed")
    v3 = gm.record_plan_version(gid, "direct", [{"index": 0}, {"index": 1}, {"index": 2}], reason="replan_world_changed")
    assert [v1["plan_version"], v2["plan_version"], v3["plan_version"]] == [1, 2, 3]
    history = gm.plan_history(gid)
    assert [h["plan_version"] for h in history] == [1, 2, 3]
    assert [h["reason"] for h in history] == ["initial_plan", "replan_task_failed", "replan_world_changed"]
    storage.close()
    cognitive.close()


def test_previous_plans_immutable(tmp_path):
    """Mutating a recorded summary must not corrupt prior versions."""
    gm, storage, cognitive = _gm(tmp_path / "i.db")
    goal = gm.create_goal("inspect")
    gid = goal.id
    summary = [{"index": 0, "intent": "list"}]
    gm.record_plan_version(gid, "direct", summary, reason="initial_plan")
    # mutate the caller's list afterwards
    summary.append({"index": 1, "intent": "read"})
    history = gm.plan_history(gid)
    assert len(history[0]["plan_summary"]) == 1  # stored version untouched
    storage.close()
    cognitive.close()


def test_replay_safety_no_duplicate_version(tmp_path):
    """Re-recording the same (strategy, plan_summary, reason) with NO task
    implementing it returns the existing version (restart replay)."""
    gm, storage, cognitive = _gm(tmp_path / "r.db")
    goal = gm.create_goal("inspect")
    gid = goal.id
    summary = [{"index": 0}]
    v1 = gm.record_plan_version(gid, "direct", summary, reason="initial_plan")
    # replay: same content, no task yet -> same version, no new row
    v1b = gm.record_plan_version(gid, "direct", summary, reason="initial_plan")
    assert v1b["plan_version"] == v1["plan_version"] == 1
    assert len(gm.plan_history(gid)) == 1
    # a task implementing v1 exists -> genuine replan creates v2 even with
    # identical content (the goal state advanced)
    task = Task(id="task_1", goal_id=gid, description="inspect", status=TaskStatus.FAILED,
                plan_version=1)
    storage.save_task(task)
    v2 = gm.record_plan_version(gid, "direct", summary, reason="replan_task_failed")
    assert v2["plan_version"] == 2
    assert len(gm.plan_history(gid)) == 2
    storage.close()
    cognitive.close()


def test_plan_history_survives_restart(tmp_path):
    db = tmp_path / "restart.db"
    gm_a, storage_a, cognitive_a = _gm(db)
    goal = gm_a.create_goal("inspect")
    gid = goal.id
    gm_a.record_plan_version(gid, "direct", [{"index": 0}], reason="initial_plan")
    gm_a.record_plan_version(gid, "avoid_known_failures", [{"index": 0}, {"index": 1}], reason="replan_task_failed")
    storage_a.close()

    gm_b, storage_b, cognitive_b = _gm(db)
    history = gm_b.plan_history(gid)
    assert [h["plan_version"] for h in history] == [1, 2]
    assert gm_b.next_plan_version(gid) == 3
    storage_b.close()


def test_task_history_and_progress(tmp_path, sandbox):
    gm, storage, cognitive = _gm(tmp_path / "t.db")
    goal = gm.create_goal("inspect")
    gid = goal.id
    storage.save_task(Task(id="t1", goal_id=gid, description="x", status=TaskStatus.COMPLETED, plan_version=1))
    storage.save_task(Task(id="t2", goal_id=gid, description="y", status=TaskStatus.FAILED, plan_version=1))
    history = gm.task_history(gid)
    assert len(history) == 2
    progress = gm.progress(gid)
    assert progress == {"goal_id": gid, "tasks": 2, "completed": 1, "failed": 1, "pending": 0}
    # pending_task returns None (both terminal)
    assert gm.pending_task(gid) is None
    storage.close()
    cognitive.close()


def test_pending_task_resume_only_for_latest_plan(tmp_path):
    gm, storage, cognitive = _gm(tmp_path / "p.db")
    goal = gm.create_goal("inspect")
    gid = goal.id
    gm.record_plan_version(gid, "direct", [{"index": 0}], reason="initial_plan")
    storage.save_task(Task(id="t_old", goal_id=gid, description="x", status=TaskStatus.PLANNED, plan_version=1))
    # latest plan is v1 -> the pending task (v1) is returned
    assert gm.pending_task(gid) is not None
    # a newer plan version v2 exists -> stale pending v1 task is NOT resumed
    gm.record_plan_version(gid, "avoid_known_failures", [{"index": 0}, {"index": 1}], reason="replan_world_changed")
    assert gm.pending_task(gid) is None
    storage.close()
    cognitive.close()
