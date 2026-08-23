"""ADR-050: managed tasks publish only after durable plan-version authority."""

from __future__ import annotations

from pathlib import Path

import pytest

from arion.capabilities.registry import CapabilityRegistry
from arion.intelligence.router import DeterministicRouter
from arion.observability.events import EventLogger
from arion.orchestration.authz import RelativePathBoundary, ResourcePolicy
from arion.orchestration.engine import ArionEngine
from arion.state.models import TaskStatus
from arion.state.store import SQLiteStorage
from tests.test_goal_run_lease import _AppendOnce, _BlockingPlanner, _close, _engine

FS = "filesystem:path"


def test_plan_claim_failure_fails_task_without_effect_then_retries_once(
    tmp_path: Path,
) -> None:
    db = tmp_path / "plan-claim-failure.db"
    effects = tmp_path / "plan-claim-effects.log"
    capability = _AppendOnce(effects)
    planner = _BlockingPlanner()
    engine, manager, storage, cognition = _engine(db, planner, capability)
    goal_id = engine.submit_goal("append exactly once").id
    original_record = manager.record_plan_version
    attempts = {"count": 0}

    def fail_once(*args, **kwargs):
        attempts["count"] += 1
        if attempts["count"] == 1:
            raise RuntimeError("goal plan persistence unavailable")
        return original_record(*args, **kwargs)

    manager.record_plan_version = fail_once
    first = engine.run_goal(goal_id)

    failed_tasks = [task for task in storage.list_tasks()
                    if task.goal_id == goal_id]
    assert first.status.value == "active"
    assert len(failed_tasks) == 1
    assert failed_tasks[0].status is TaskStatus.FAILED
    assert failed_tasks[0].plan_version is None
    assert "planning persistence failed" in (failed_tasks[0].error or "")
    assert manager.plan_history(goal_id) == []
    assert capability.calls == 0
    assert not effects.exists()
    assert not storage.list_checkpoints(failed_tasks[0].id)

    second = engine.run_goal(goal_id)
    assert second.status.value == "completed"
    assert attempts["count"] == 2
    assert planner.calls == 2
    assert capability.calls == 1
    assert effects.read_text(encoding="utf-8").splitlines() == ["effect-1"]
    exact = [task for task in storage.list_tasks()
             if task.goal_id == goal_id and task.plan_version == 1]
    assert len(exact) == 1 and exact[0].status is TaskStatus.COMPLETED
    _close(engine, storage, cognition)


def test_plan_commit_then_task_save_failure_reconstructs_once_after_restart(
    tmp_path: Path,
) -> None:
    db = tmp_path / "plan-before-task-save.db"
    effects = tmp_path / "plan-before-task-effects.log"
    first_capability = _AppendOnce(effects)
    first_planner = _BlockingPlanner()
    first, first_manager, first_storage, first_cognition = _engine(
        db, first_planner, first_capability
    )
    goal_id = first.submit_goal("append exactly once").id
    original_save = first_storage.save_task

    def fail_planned_save(task):
        if task.status is TaskStatus.PLANNED and task.plan_version is not None:
            raise RuntimeError("task publication unavailable")
        return original_save(task)

    first_storage.save_task = fail_planned_save
    with pytest.raises(RuntimeError, match="task publication unavailable"):
        first.run_goal(goal_id)
    first_storage.save_task = original_save

    plans = first_manager.plan_history(goal_id)
    tasks = [task for task in first_storage.list_tasks()
             if task.goal_id == goal_id]
    assert [plan["plan_version"] for plan in plans] == [1]
    assert len(tasks) == 1
    assert tasks[0].status is TaskStatus.CREATED
    assert tasks[0].steps == []
    assert tasks[0].plan_version is None
    assert first_capability.calls == 0
    assert not effects.exists()
    _close(first, first_storage, first_cognition)

    resumed_capability = _AppendOnce(effects)
    resumed, resumed_manager, resumed_storage, resumed_cognition = _engine(
        db, _BlockingPlanner(forbidden=True), resumed_capability
    )
    result = resumed.run_goal(goal_id)

    assert result.status.value == "completed"
    assert resumed_capability.calls == 1
    assert effects.read_text(encoding="utf-8").splitlines() == ["effect-1"]
    exact = [task for task in resumed_storage.list_tasks()
             if task.goal_id == goal_id and task.plan_version == 1]
    assert len(exact) == 1
    assert exact[0].status is TaskStatus.COMPLETED
    _close(resumed, resumed_storage, resumed_cognition)


def test_normal_managed_plan_publishes_version_with_executable_task(
    tmp_path: Path,
) -> None:
    db = tmp_path / "normal-publication.db"
    effects = tmp_path / "normal-publication.log"
    capability = _AppendOnce(effects)
    engine, manager, storage, cognition = _engine(
        db, _BlockingPlanner(), capability
    )
    goal_id = engine.submit_goal("append exactly once").id

    result = engine.run_goal(goal_id)

    task = next(task for task in storage.list_tasks()
                if task.goal_id == goal_id)
    assert result.status.value == "completed"
    assert task.plan_version == 1
    assert task.status is TaskStatus.COMPLETED
    assert [plan["plan_version"] for plan in manager.plan_history(goal_id)] == [1]
    kinds = [event.kind for event in storage.list_events(task.id)]
    assert kinds.index("plan.produced") < kinds.index("step.started")
    assert capability.calls == 1
    _close(engine, storage, cognition)


def test_standalone_engine_retains_unversioned_task_compatibility(
    tmp_path: Path,
) -> None:
    db = tmp_path / "standalone.db"
    effects = tmp_path / "standalone.log"
    capability = _AppendOnce(effects)
    storage = SQLiteStorage(db)
    registry = CapabilityRegistry()
    registry.register(capability)
    planner = _BlockingPlanner()
    engine = ArionEngine(
        storage=storage,
        registry=registry,
        planner=planner,
        router=DeterministicRouter(planner),
        events=EventLogger(sinks=[storage]),
        policy=ResourcePolicy(
            allowed_scopes={"goal:write"},
            risk_deny=set(),
            risk_approve=set(),
            boundaries={FS: RelativePathBoundary()},
        ),
    )

    task = engine.execute_goal("append exactly once")

    assert task.status is TaskStatus.COMPLETED
    assert task.plan_version is None
    assert capability.calls == 1
    engine.shutdown(); storage.close()
