"""ADR-040: task revisions, terminal fencing, and same-step ownership."""

from __future__ import annotations

import json
import sqlite3
import threading
import time
from pathlib import Path

import pytest

from arion.bootstrap import build_engine
from arion.capabilities.registry import (
    ActionSpec,
    CapabilityError,
    CapabilityRegistry,
)
from arion.intelligence.planner import DeterministicPlanner
from arion.intelligence.router import DeterministicRouter
from arion.observability.events import EventLogger
from arion.orchestration.authz import ResourcePolicy
from arion.orchestration.engine import ArionEngine
from arion.state.models import (
    Checkpoint,
    PlanStep,
    StepStatus,
    Task,
    TaskStateError,
    TaskStatus,
    VerificationPolicy,
    utcnow,
)
from arion.state.scheduler_work import SchedulerWorkStatus
from arion.state.store import SQLiteStorage


class _CountRead:
    name = "lifecycle.read"
    description = "count read executions"
    actions = [ActionSpec(
        name="read",
        description="read",
        required_scope="lifecycle:read",
        risk="low",
        side_effects="read_only",
        retry_safe=True,
    )]

    def __init__(self) -> None:
        self.calls = 0

    def execute(self, action, params):
        self.calls += 1
        return {"ok": True, "call": self.calls}


class _BlockingRead(_CountRead):
    def __init__(self) -> None:
        super().__init__()
        self.started = threading.Event()
        self.release = threading.Event()
        self._lock = threading.Lock()
        self.active = 0
        self.max_active = 0

    def execute(self, action, params):
        with self._lock:
            self.calls += 1
            self.active += 1
            self.max_active = max(self.max_active, self.active)
            call = self.calls
        try:
            self.started.set()
            if call == 1:
                self.release.wait(timeout=5)
            return {"ok": True, "call": call}
        finally:
            with self._lock:
                self.active -= 1


class _FailThenSucceedMutation:
    name = "lifecycle.mutate"
    description = "uncertain first mutation"
    actions = [ActionSpec(
        name="mutate",
        description="mutate",
        required_scope="lifecycle:write",
        risk="low",
        side_effects="mutating",
        retry_safe=False,
        idempotent=False,
    )]

    def __init__(self) -> None:
        self.calls = 0

    def execute(self, action, params):
        self.calls += 1
        if self.calls == 1:
            raise CapabilityError("uncertain external result")
        return {"ok": True}


class _BlockingPlanner:
    def __init__(self) -> None:
        self.started = threading.Event()
        self.release = threading.Event()

    def required_capabilities(self, goal_description):
        return {"filesystem.read"}

    def plan(self, goal_description, task_id, registry, context=None):
        self.started.set()
        self.release.wait(timeout=5)
        return [PlanStep(
            index=0,
            intent="read",
            capability="filesystem.read",
            action="read",
            scope="filesystem:read",
            params={"path": "README.md"},
            verification=VerificationPolicy("non_empty"),
            max_attempts=1,
        )]


class _Crash(BaseException):
    pass


def _engine(
    db: Path,
    capability,
    scope: str,
    *,
    scheduler_lease_seconds: float = 300.0,
) -> ArionEngine:
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
        policy=ResourcePolicy(allowed_scopes={scope}),
        scheduler_lease_seconds=scheduler_lease_seconds,
        scheduler_max_lease_seconds=scheduler_lease_seconds * 2,
        scheduler_reclaim_on_start=False,
        lock_wait_max_seconds=0,
    )


def _seed(engine: ArionEngine, task_id: str, capability, scope: str) -> Task:
    goal = engine.submit_goal("task lifecycle test")
    task = Task(id=task_id, goal_id=goal.id, description="task lifecycle test")
    task.steps = [PlanStep(
        index=0,
        intent="execute once",
        capability=capability.name,
        action=capability.actions[0].name,
        scope=scope,
        verification=VerificationPolicy("non_empty"),
        max_attempts=1,
    )]
    engine.storage.save_task(task)
    return task


def test_concurrent_terminal_task_writes_have_one_cas_winner(tmp_path: Path) -> None:
    db = tmp_path / "terminal-race.db"
    left = SQLiteStorage(db)
    right = SQLiteStorage(db)
    seed = Task(id="task-race", goal_id="goal", description="race")
    left.save_task(seed)
    complete = left.load_task(seed.id)
    fail = right.load_task(seed.id)
    complete.status = TaskStatus.COMPLETED
    complete.completed_at = utcnow()
    fail.status = TaskStatus.FAILED
    fail.error = "competing failure"
    fail.completed_at = utcnow()
    barrier = threading.Barrier(2)
    committed: list[str] = []
    errors: list[BaseException] = []

    def save(store, task):
        barrier.wait()
        try:
            store.save_task(task)
            committed.append(task.status.value)
        except BaseException as exc:
            errors.append(exc)

    a = threading.Thread(target=save, args=(left, complete))
    b = threading.Thread(target=save, args=(right, fail))
    a.start(); b.start(); a.join(); b.join()

    durable = left.load_task(seed.id)
    assert len(committed) == 1
    assert len(errors) == 1 and isinstance(errors[0], TaskStateError)
    assert durable.status.value == committed[0]
    assert durable.status in (TaskStatus.COMPLETED, TaskStatus.FAILED)
    immutable = right.load_task(seed.id)
    immutable.description = "rewrite terminal payload"
    with pytest.raises(TaskStateError, match="immutable"):
        right.save_task(immutable)
    stale = right.load_task(seed.id)
    stale.status = TaskStatus.RUNNING
    with pytest.raises(TaskStateError, match="terminal task"):
        right.save_task(stale)
    left.close(); right.close()


def test_current_writer_cannot_regress_running_task_to_planning(
    tmp_path: Path,
) -> None:
    storage = SQLiteStorage(tmp_path / "invalid-transition.db")
    task = Task(id="task-transition", goal_id="goal", description="transition")
    storage.save_task(task)
    task.status = TaskStatus.RUNNING
    storage.save_task(task)
    current = storage.load_task(task.id)
    current.status = TaskStatus.PLANNED
    with pytest.raises(TaskStateError, match="invalid task transition"):
        storage.save_task(current)
    assert storage.load_task(task.id).status is TaskStatus.RUNNING
    storage.close()


def test_legacy_task_rows_gain_revision_without_snapshot_rewrite(
    tmp_path: Path,
) -> None:
    db = tmp_path / "legacy.db"
    connection = sqlite3.connect(db)
    connection.execute(
        "CREATE TABLE tasks (id TEXT PRIMARY KEY, goal_id TEXT NOT NULL, "
        "description TEXT NOT NULL, status TEXT NOT NULL, snapshot TEXT NOT NULL, "
        "updated_at TEXT NOT NULL)"
    )
    legacy = Task(id="legacy", goal_id="goal", description="legacy")
    snapshot = legacy.to_dict()
    snapshot.pop("revision", None)
    connection.execute(
        "INSERT INTO tasks (id, goal_id, description, status, snapshot, updated_at) "
        "VALUES (?,?,?,?,?,?)",
        (legacy.id, legacy.goal_id, legacy.description, legacy.status.value,
         json.dumps(snapshot), legacy.updated_at),
    )
    connection.commit(); connection.close()

    storage = SQLiteStorage(db)
    loaded = storage.load_task(legacy.id)
    # ADR-041 promotes the task-table generation without rewriting the legacy
    # JSON payload; the column remains authoritative.
    assert loaded.revision == 1
    storage.save_task(loaded)
    assert storage.load_task(legacy.id).revision == 2
    storage.close()


def test_checkpoint_cannot_mint_revision_or_regress_completed_step(
    tmp_path: Path,
) -> None:
    capability = _CountRead()
    engine = _engine(tmp_path / "checkpoint-forged.db", capability,
                     "lifecycle:read")
    goal = engine.submit_goal("two-step checkpoint test")
    task = Task(id="checkpoint-forged", goal_id=goal.id,
                description="two-step checkpoint test")
    task.steps = [
        PlanStep(index=index, intent=f"step {index}",
                 capability=capability.name, action="read",
                 scope="lifecycle:read",
                 verification=VerificationPolicy("non_empty"), max_attempts=1)
        for index in range(2)
    ]
    engine.storage.save_task(task)
    stale = Task.from_dict(task.to_dict())
    task.status = TaskStatus.RUNNING
    task.current_step = 1
    task.steps[0].status = StepStatus.SUCCEEDED
    task.steps[0].result = {"ok": True, "seeded": True}
    engine.storage.save_task(task)
    stale.revision = task.revision + 100  # checkpoint cannot mint authority
    engine.storage.save_checkpoint(Checkpoint(
        task_id=task.id,
        status=TaskStatus.RUNNING.value,
        step_index=0,
        snapshot=stale.to_dict(),
        reason="forged higher revision",
        created_at=utcnow(),
    ))

    result = engine.run_task(task.id)
    assert result.status is TaskStatus.COMPLETED
    assert result.steps[0].result == {"ok": True, "seeded": True}
    assert result.steps[1].status is StepStatus.SUCCEEDED
    assert capability.calls == 1
    engine.shutdown(); engine.storage.close()


def test_late_stale_checkpoint_cannot_replay_completed_step(
    tmp_path: Path,
) -> None:
    capability = _CountRead()
    engine = _engine(tmp_path / "checkpoint.db", capability, "lifecycle:read")
    task = _seed(engine, "checkpoint-task", capability, "lifecycle:read")
    stale = engine.storage.load_task(task.id)

    assert engine.run_task(task.id).status is TaskStatus.COMPLETED
    assert capability.calls == 1
    engine.storage.save_checkpoint(Checkpoint(
        task_id=task.id,
        status=TaskStatus.RUNNING.value,
        step_index=0,
        snapshot=stale.to_dict(),
        reason="late stale worker",
        created_at=utcnow(),
    ))

    resumed = engine.run_task(task.id)
    assert resumed.status is TaskStatus.COMPLETED
    assert resumed.steps[0].status is StepStatus.SUCCEEDED
    assert capability.calls == 1
    engine.shutdown(); engine.storage.close()


def test_task_save_before_checkpoint_crash_resumes_without_replay(
    tmp_path: Path,
) -> None:
    db = tmp_path / "checkpoint-gap.db"
    capability = _CountRead()
    first = _engine(db, capability, "lifecycle:read")
    task = _seed(first, "checkpoint-gap", capability, "lifecycle:read")
    original_checkpoint = first._checkpoint

    def crash_after_task_save(candidate: Task, reason: str) -> None:
        if reason == "step completed":
            raise _Crash("task saved before checkpoint")
        original_checkpoint(candidate, reason)

    first._checkpoint = crash_after_task_save
    with pytest.raises(_Crash):
        first.run_task(task.id)
    mid = first.storage.load_task(task.id)
    assert mid.status is TaskStatus.RUNNING
    assert mid.steps[0].status is StepStatus.SUCCEEDED
    assert capability.calls == 1
    first.shutdown(); first.storage.close()

    resumed = _engine(db, capability, "lifecycle:read")
    assert resumed.run_task(task.id).status is TaskStatus.COMPLETED
    assert capability.calls == 1
    resumed.shutdown(); resumed.storage.close()


def test_terminal_task_save_before_event_crash_remains_terminal(
    tmp_path: Path,
) -> None:
    db = tmp_path / "event-gap.db"
    capability = _CountRead()
    first = _engine(db, capability, "lifecycle:read")
    task = _seed(first, "event-gap", capability, "lifecycle:read")
    original_emit = first._emit

    def crash_after_terminal_save(kind, *args, **kwargs):
        if kind == "task.completed":
            raise _Crash("terminal task saved before event")
        return original_emit(kind, *args, **kwargs)

    first._emit = crash_after_terminal_save
    with pytest.raises(_Crash):
        first.run_task(task.id)
    assert first.storage.load_task(task.id).status is TaskStatus.COMPLETED
    assert capability.calls == 1
    first.shutdown(); first.storage.close()

    resumed = _engine(db, capability, "lifecycle:read")
    assert resumed.run_task(task.id).status is TaskStatus.COMPLETED
    assert capability.calls == 1
    resumed.shutdown(); resumed.storage.close()


def test_live_same_task_owner_is_renewed_and_not_preempted(
    tmp_path: Path,
) -> None:
    db = tmp_path / "live-owner.db"
    capability = _BlockingRead()
    first = _engine(
        db, capability, "lifecycle:read", scheduler_lease_seconds=0.12
    )
    task = _seed(first, "shared-task", capability, "lifecycle:read")
    second = _engine(
        db, capability, "lifecycle:read", scheduler_lease_seconds=0.12
    )
    first_result: dict[str, Task] = {}
    first_error: list[BaseException] = []

    def run_first() -> None:
        try:
            first_result["task"] = first.run_task(task.id)
        except BaseException as exc:
            first_error.append(exc)

    worker = threading.Thread(target=run_first)
    worker.start()
    assert capability.started.wait(timeout=3)
    time.sleep(0.3)  # beyond the original lease; heartbeat must retain ownership

    observed = second.run_task(task.id)
    rows = first.storage.list_work(task_id=task.id, step_index=0)
    assert observed.status not in (TaskStatus.COMPLETED, TaskStatus.FAILED)
    assert capability.calls == 1
    assert capability.max_active == 1
    assert [row.status for row in rows] == [SchedulerWorkStatus.RUNNING]

    capability.release.set()
    worker.join(timeout=5)
    assert not first_error
    assert first_result["task"].status is TaskStatus.COMPLETED
    assert second.run_task(task.id).status is TaskStatus.COMPLETED
    assert capability.calls == 1
    assert all(
        row.status is not SchedulerWorkStatus.ABANDONED
        for row in first.storage.list_work(task_id=task.id)
    )
    first.shutdown(); second.shutdown()
    first.storage.close(); second.storage.close()


def test_goal_cancellation_during_planning_prevents_dispatch(
    tmp_path: Path,
) -> None:
    sandbox = tmp_path / "sandbox-plan"
    sandbox.mkdir()
    (sandbox / "README.md").write_text("ok", encoding="utf-8")
    planner = _BlockingPlanner()
    engine = build_engine(tmp_path / "planning.db", sandbox, planner=planner)
    goal = engine.submit_goal("plan then read")
    task = engine.create_task(goal)
    result: dict[str, Task] = {}

    worker = threading.Thread(
        target=lambda: result.setdefault("task", engine.run_task(task.id))
    )
    worker.start()
    assert planner.started.wait(timeout=3)
    engine.goal_manager.cancel(goal.id, reason="operator cancelled")
    planner.release.set()
    worker.join(timeout=5)

    durable = engine.storage.load_task(task.id)
    assert engine.goal_manager.get_goal(goal.id).status.value == "cancelled"
    assert durable.status is TaskStatus.FAILED
    assert not durable.steps
    assert not [
        event for event in engine.storage.list_events(task.id)
        if event.kind == "capability.executed"
    ]
    engine.shutdown()


def test_goal_cancellation_during_execution_cannot_complete_task(
    tmp_path: Path,
) -> None:
    sandbox = tmp_path / "sandbox-exec"
    sandbox.mkdir()
    (sandbox / "README.md").write_text("ok", encoding="utf-8")
    capability = _BlockingRead()
    engine = build_engine(
        tmp_path / "execution.db",
        sandbox,
        policy=ResourcePolicy(allowed_scopes={"lifecycle:read"}),
    )
    engine.registry.register(capability)
    goal = engine.submit_goal("blocking execution")
    task = engine.create_task(goal)
    task.steps = [PlanStep(
        index=0,
        intent="block",
        capability=capability.name,
        action="read",
        scope="lifecycle:read",
        verification=VerificationPolicy("non_empty"),
        max_attempts=1,
    )]
    engine.storage.save_task(task)
    result: dict[str, Task] = {}

    worker = threading.Thread(
        target=lambda: result.setdefault("task", engine.run_task(task.id))
    )
    worker.start()
    assert capability.started.wait(timeout=3)
    engine.goal_manager.cancel(goal.id, reason="operator cancelled")
    capability.release.set()
    worker.join(timeout=5)

    durable = engine.storage.load_task(task.id)
    assert capability.calls == 1  # in-flight code is non-preemptible
    assert durable.steps[0].status is StepStatus.SUCCEEDED
    assert durable.status is TaskStatus.FAILED
    assert "terminal goal" in (durable.error or "")
    assert engine.goal_manager.get_goal(goal.id).status.value == "cancelled"
    engine.shutdown()


def test_required_recovery_fences_task_after_split_crash(
    tmp_path: Path,
) -> None:
    db = tmp_path / "recovery-gap.db"
    capability = _FailThenSucceedMutation()
    first = _engine(db, capability, "lifecycle:write")
    task = _seed(first, "recovery-gap", capability, "lifecycle:write")
    original_save = first.storage.save_task
    original_commit = first.storage.commit_recovery_requirement

    def split_after_recovery(recovery, candidate, expected_revision):
        first.storage.create_recovery(recovery)
        raise _Crash("recovery committed before task snapshot")

    def crash_failed_snapshot(candidate: Task) -> None:
        if (candidate.steps
                and candidate.steps[0].status is StepStatus.FAILED):
            raise _Crash("recovery committed before task snapshot")
        original_save(candidate)

    first.storage.commit_recovery_requirement = split_after_recovery
    first.storage.save_task = crash_failed_snapshot
    with pytest.raises(_Crash):
        first.run_task(task.id)
    first.storage.commit_recovery_requirement = original_commit
    first.storage.save_task = original_save
    mid = first.storage.load_task(task.id)
    recoveries = first.storage.list_recoveries(task_id=task.id)
    assert mid.steps[0].status is StepStatus.PENDING
    assert [record.status.value for record in recoveries] == ["required"]
    assert capability.calls == 1
    first.shutdown(); first.storage.close()

    resumed = _engine(db, capability, "lifecycle:write")
    result = resumed.run_task(task.id)
    assert result.status is TaskStatus.FAILED
    assert result.steps[0].status is StepStatus.FAILED
    assert "recovery required" in (result.error or "")
    assert capability.calls == 1
    resumed.shutdown(); resumed.storage.close()
