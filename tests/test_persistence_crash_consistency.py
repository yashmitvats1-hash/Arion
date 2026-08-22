"""ADR-041: cross-record crash consistency and restart convergence."""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path

import pytest

from arion.capabilities.registry import ActionSpec, CapabilityRegistry
from arion.intelligence.planner import DeterministicPlanner
from arion.intelligence.router import DeterministicRouter
from arion.observability.events import EventLogger
from arion.orchestration.authz import ApprovalOutcome, ResourcePolicy
from arion.orchestration.engine import ArionEngine
from arion.state.models import (
    Checkpoint,
    PlanStep,
    StepStatus,
    Task,
    TaskStatus,
    VerificationPolicy,
)
from arion.state.recovery import RecoveryStatus
from arion.state.scheduler_work import SchedulerWorkStatus
from arion.state.store import SQLiteStorage
from tests.test_atomic_approval import _engine as approval_engine
from tests.test_recovery_fencing import (
    _engine as recovery_engine,
    _fail_mutation,
)
from tests.test_task_lifecycle_fencing import (
    _CountRead,
    _Crash,
    _FailThenSucceedMutation,
    _engine as lifecycle_engine,
    _seed as lifecycle_seed,
)


def _close(engine: ArionEngine, storage=None) -> None:
    try:
        engine.shutdown()
    finally:
        try:
            (storage or engine.storage).close()
        except Exception:
            pass


def test_atomic_recovery_requirement_terminalizes_task(tmp_path: Path) -> None:
    capability = _FailThenSucceedMutation()
    engine = lifecycle_engine(
        tmp_path / "atomic-recovery.db", capability, "lifecycle:write"
    )
    task = lifecycle_seed(
        engine, "atomic-recovery", capability, "lifecycle:write"
    )
    initial_revision = task.revision

    result = engine.run_task(task.id)

    durable = engine.storage.load_task(task.id)
    recoveries = engine.storage.list_recoveries(task_id=task.id)
    assert result.status is TaskStatus.FAILED
    assert durable.status is TaskStatus.FAILED
    assert durable.steps[0].status is StepStatus.FAILED
    assert durable.revision > initial_revision
    assert len(recoveries) == 1
    assert recoveries[0].status is RecoveryStatus.REQUIRED
    assert capability.calls == 1
    _close(engine)


def test_task_update_abort_retains_recovery_and_prevents_replay(
    tmp_path: Path,
) -> None:
    db = tmp_path / "recovery-task-abort.db"
    capability = _FailThenSucceedMutation()
    first = lifecycle_engine(db, capability, "lifecycle:write")
    task = lifecycle_seed(
        first, "recovery-task-abort", capability, "lifecycle:write"
    )
    first.storage._conn.execute(
        "CREATE TRIGGER abort_failed_task BEFORE UPDATE OF status ON tasks "
        "WHEN NEW.status='failed' "
        "BEGIN SELECT RAISE(ABORT, 'task mirror unavailable'); END"
    )
    first.storage._conn.commit()

    with pytest.raises(sqlite3.IntegrityError):
        first.run_task(task.id)

    # Combined transaction rolled back, then the recovery-only fallback won.
    assert first.storage.load_task(task.id).steps[0].status is StepStatus.PENDING
    recoveries = first.storage.list_recoveries(task_id=task.id)
    assert len(recoveries) == 1
    assert recoveries[0].status is RecoveryStatus.REQUIRED
    assert capability.calls == 1
    first.storage._conn.execute("DROP TRIGGER abort_failed_task")
    first.storage._conn.commit()
    _close(first)

    resumed = lifecycle_engine(db, capability, "lifecycle:write")
    result = resumed.run_task(task.id)
    assert result.status is TaskStatus.FAILED
    assert capability.calls == 1
    _close(resumed)


def test_recovery_table_failure_uses_task_marker_then_repairs(
    tmp_path: Path,
) -> None:
    db = tmp_path / "recovery-table-abort.db"
    capability = _FailThenSucceedMutation()
    engine = lifecycle_engine(db, capability, "lifecycle:write")
    task = lifecycle_seed(
        engine, "recovery-table-abort", capability, "lifecycle:write"
    )
    engine.storage._conn.execute(
        "CREATE TRIGGER abort_recovery BEFORE INSERT ON mutation_recoveries "
        "BEGIN SELECT RAISE(ABORT, 'recovery table unavailable'); END"
    )
    engine.storage._conn.commit()

    with pytest.raises(sqlite3.IntegrityError):
        engine.run_task(task.id)

    durable = engine.storage.load_task(task.id)
    assert durable.status is TaskStatus.FAILED
    assert "recovery persistence failed" in (durable.error or "")
    assert engine.storage.list_recoveries(task_id=task.id) == []
    assert capability.calls == 1
    # Direct resume is terminal and cannot repeat the mutation.
    assert engine.run_task(task.id).status is TaskStatus.FAILED
    assert capability.calls == 1

    engine.storage._conn.execute("DROP TRIGGER abort_recovery")
    engine.storage._conn.commit()
    engine._reconcile_missing_recovery_records(task.goal_id)
    repaired = engine.storage.list_recoveries(task_id=task.id)
    assert len(repaired) == 1
    assert repaired[0].status is RecoveryStatus.REQUIRED
    _close(engine)


def test_acknowledged_recovery_repairs_stale_goal_blocker(
    tmp_path: Path,
) -> None:
    db = tmp_path / "ack-blocker.db"
    engine, manager, storage, _, sandbox, goal_id, recovery = _fail_mutation(
        tmp_path, db=db
    )
    original_clear = manager.clear_blocker

    def fail_cleanup(*args, **kwargs):
        raise RuntimeError("goal cleanup unavailable")

    manager.clear_blocker = fail_cleanup
    acknowledged = engine.acknowledge_recovery(
        recovery.recovery_id, actor="operator"
    )
    manager.clear_blocker = original_clear
    assert acknowledged.status is RecoveryStatus.ACKNOWLEDGED
    assert any(
        (blocker.get("type") or blocker.get("key")) == "recovery_required"
        for blocker in manager.get_goal(goal_id).blockers
    )
    _close(engine, storage)

    resumed, resumed_manager, resumed_storage, _ = recovery_engine(db, sandbox)
    assert resumed_manager.recheck_blockers(goal_id)
    repaired_goal = resumed_manager.get_goal(goal_id)
    assert repaired_goal.status.value == "active"
    assert not any(
        (blocker.get("type") or blocker.get("key")) == "recovery_required"
        for blocker in repaired_goal.blockers
    )
    _close(resumed, resumed_storage)


class _ParamCapability:
    name = "legacy.param"
    description = "records executed legacy params"
    actions = [ActionSpec(
        name="run",
        description="run",
        required_scope="legacy:run",
        side_effects="read_only",
        retry_safe=True,
    )]

    def __init__(self) -> None:
        self.values: list[str] = []

    def execute(self, action, params):
        self.values.append(params["value"])
        return {"value": params["value"]}


def _param_engine(db: Path, capability: _ParamCapability) -> ArionEngine:
    storage = SQLiteStorage(db)
    registry = CapabilityRegistry()
    registry.register(capability)
    planner = DeterministicPlanner()
    return ArionEngine(
        storage=storage,
        registry=registry,
        planner=planner,
        router=DeterministicRouter(planner),
        events=EventLogger(sinks=[storage]),
        policy=ResourcePolicy(allowed_scopes={"legacy:run"}),
        scheduler_reclaim_on_start=False,
    )


def test_startup_promotes_legacy_zero_and_ignores_stale_params(
    tmp_path: Path,
) -> None:
    db = tmp_path / "legacy-zero.db"
    capability = _ParamCapability()
    seed_engine = _param_engine(db, capability)
    goal = seed_engine.submit_goal("legacy zero")
    task = Task(
        id="legacy-zero",
        goal_id=goal.id,
        description="legacy zero",
        status=TaskStatus.RUNNING,
        steps=[PlanStep(
            index=0,
            intent="current",
            capability=capability.name,
            action="run",
            scope="legacy:run",
            params={"value": "NEW-DURABLE"},
            verification=VerificationPolicy("non_empty"),
            max_attempts=1,
        )],
    )
    seed_engine.storage.save_task(task)
    durable_payload = seed_engine.storage.load_task(task.id).to_dict()
    durable_payload["revision"] = 0
    seed_engine.storage._conn.execute(
        "UPDATE tasks SET snapshot=?, revision=0, updated_at=? WHERE id=?",
        (json.dumps(durable_payload), "2026-01-01T00:00:00+00:00", task.id),
    )
    stale = Task.from_dict(durable_payload)
    stale.steps[0].params = {"value": "OLD-CHECKPOINT"}
    seed_engine.storage.save_checkpoint(Checkpoint(
        id="legacy-stale",
        task_id=task.id,
        status=TaskStatus.RUNNING.value,
        step_index=0,
        snapshot=stale.to_dict(),
        reason="legacy stale",
        created_at="2026-02-01T00:00:00+00:00",
    ))
    _close(seed_engine)

    resumed = _param_engine(db, capability)
    assert resumed.storage.load_task(task.id).revision == 1
    result = resumed.run_task(task.id)
    assert result.status is TaskStatus.COMPLETED
    assert capability.values == ["NEW-DURABLE"]
    assert result.steps[0].params == {"value": "NEW-DURABLE"}
    _close(resumed)


def test_task_persistence_failure_cannot_publish_completed_work(
    tmp_path: Path,
) -> None:
    capability = _CountRead()
    engine = lifecycle_engine(
        tmp_path / "task-before-work.db", capability, "lifecycle:read"
    )
    task = lifecycle_seed(
        engine, "task-before-work", capability, "lifecycle:read"
    )
    original_save = engine.storage.save_task

    def fail_result(candidate: Task) -> None:
        if candidate.steps[0].status is StepStatus.SUCCEEDED:
            raise _Crash("task result unavailable")
        original_save(candidate)

    engine.storage.save_task = fail_result
    with pytest.raises(_Crash):
        engine.run_task(task.id)
    engine.storage.save_task = original_save

    durable = engine.storage.load_task(task.id)
    rows = engine.storage.list_work(task_id=task.id, step_index=0)
    assert durable.steps[0].status is StepStatus.PENDING
    assert [row.status for row in rows] == [SchedulerWorkStatus.FAILED]
    assert capability.calls == 1
    _close(engine)


def test_scheduler_terminal_failure_after_task_save_does_not_replay(
    tmp_path: Path,
) -> None:
    db = tmp_path / "work-terminal-failure.db"
    capability = _CountRead()
    first = lifecycle_engine(db, capability, "lifecycle:read")
    task = lifecycle_seed(
        first, "work-terminal-failure", capability, "lifecycle:read"
    )
    original_terminal = first.scheduler_registry.mark_terminal

    def fail_terminal(*args, **kwargs):
        raise _Crash("scheduler terminal unavailable")

    first.scheduler_registry.mark_terminal = fail_terminal
    with pytest.raises(_Crash):
        first.run_task(task.id)
    first.scheduler_registry.mark_terminal = original_terminal
    durable = first.storage.load_task(task.id)
    assert durable.steps[0].status is StepStatus.SUCCEEDED
    assert capability.calls == 1
    _close(first)

    resumed = lifecycle_engine(db, capability, "lifecycle:read")
    result = resumed.run_task(task.id)
    assert result.status is TaskStatus.COMPLETED
    assert capability.calls == 1
    _close(resumed)


def test_prune_abort_rolls_back_whole_delete(tmp_path: Path) -> None:
    storage = SQLiteStorage(tmp_path / "prune-abort.db")
    task = Task(id="prune-task", goal_id="goal", description="prune")
    for index in range(6):
        storage.save_checkpoint(Checkpoint(
            id=f"checkpoint-{index}",
            task_id=task.id,
            status=TaskStatus.RUNNING.value,
            step_index=index,
            snapshot=task.to_dict(),
            reason=str(index),
        ))
    storage._conn.execute(
        "CREATE TRIGGER abort_prune BEFORE DELETE ON checkpoints "
        "BEGIN SELECT RAISE(ABORT, 'interrupted prune'); END"
    )
    storage._conn.commit()

    with pytest.raises(sqlite3.IntegrityError):
        storage.prune_checkpoints(task.id, keep_last=2)

    retained = storage.list_checkpoints(task.id)
    assert len(retained) == 6
    assert storage.latest_checkpoint(task.id).id == "checkpoint-5"
    storage.close()


def test_approval_decision_sqlite_abort_rolls_back_both_rows(
    tmp_path: Path,
) -> None:
    engine, storage, capability, cognitive = approval_engine(
        tmp_path / "approval-abort.db"
    )
    task = engine.execute_goal("approval rollback")
    request = storage.list_requests(status="pending")[0]
    storage._conn.execute(
        "CREATE TRIGGER abort_approval_task BEFORE UPDATE OF status ON tasks "
        "WHEN NEW.status='running' "
        "BEGIN SELECT RAISE(ABORT, 'task update unavailable'); END"
    )
    storage._conn.commit()

    with pytest.raises(sqlite3.IntegrityError):
        engine.resolve_approval_request(
            request.approval_id, ApprovalOutcome.APPROVED
        )

    assert storage.get_request(request.approval_id).status.value == "pending"
    assert storage.load_task(task.id).status is TaskStatus.AWAITING_APPROVAL
    assert capability.calls == 0
    _close(engine, storage)
    if cognitive is not None:
        cognitive.close()
