"""ADR-036: bounded full checkpoints preserve recovery correctness."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from arion.capabilities.filesystem import FilesystemReadCapability
from arion.capabilities.registry import ActionSpec, CapabilityRegistry
from arion.intelligence.planner import DeterministicPlanner
from arion.intelligence.router import DeterministicRouter
from arion.observability.events import EventLogger
from arion.orchestration.authz import RelativePathBoundary, ResourcePolicy
from arion.orchestration.engine import ArionEngine
from arion.state.models import (
    Checkpoint,
    PlanStep,
    StepStatus,
    Task,
    TaskStatus,
    VerificationPolicy,
)
from arion.state.store import DEFAULT_CHECKPOINT_RETENTION, SQLiteStorage


class _PayloadCapability:
    name = "checkpoint.payload"
    description = "deterministic checkpoint payload"
    actions = [ActionSpec(
        name="get",
        description="get payload",
        required_scope="checkpoint:read",
        param_schema={"index": {"type": "integer", "required": True}},
    )]

    def __init__(self, payload_size: int = 1024):
        self.payload_size = payload_size
        self.calls: list[int] = []

    def execute(self, action, params):
        index = params["index"]
        self.calls.append(index)
        return {"body": f"payload-{index}:" + "x" * self.payload_size,
                "index": index}


class _ManyStepPlanner:
    def __init__(self, count: int, *, capability: str = "checkpoint.payload",
                 action: str = "get", scope: str = "checkpoint:read"):
        self.count = count
        self.capability = capability
        self.action = action
        self.scope = scope

    def required_capabilities(self, goal_description):
        return {self.capability}

    def plan(self, goal_description, task_id, registry, context=None):
        return [PlanStep(
            index=index,
            intent=f"step {index}",
            capability=self.capability,
            action=self.action,
            scope=self.scope,
            params={"index": index},
            verification=VerificationPolicy("schema_keys", {"keys": ["body"]}),
            max_attempts=1,
        ) for index in range(self.count)]


def _engine(tmp_path: Path, capability, planner, *, db_name="arion.db",
            scope="checkpoint:read"):
    storage = SQLiteStorage(tmp_path / db_name)
    registry = CapabilityRegistry()
    registry.register(capability)
    events = EventLogger(sinks=[storage])
    engine = ArionEngine(
        storage=storage,
        registry=registry,
        planner=planner,
        router=DeterministicRouter(DeterministicPlanner()),
        events=events,
        policy=ResourcePolicy(allowed_scopes={scope}),
        max_concurrency=1,
    )
    return engine, storage


def test_store_pruning_keeps_newest_full_checkpoints() -> None:
    storage = SQLiteStorage(":memory:")
    task = Task(id="task-retention", goal_id="goal", description="retention")
    for index in range(12):
        snapshot = task.to_dict()
        snapshot["current_step"] = index
        storage.save_checkpoint(Checkpoint(
            id=f"ckpt-{index:02d}",
            task_id=task.id,
            status="running",
            step_index=index,
            snapshot=snapshot,
            reason=f"step {index}",
        ))

    removed = storage.prune_checkpoints(task.id, keep_last=3)
    retained = storage.list_checkpoints(task.id)

    assert removed == 9
    assert [checkpoint.id for checkpoint in retained] == [
        "ckpt-09", "ckpt-10", "ckpt-11"
    ]
    assert storage.latest_checkpoint(task.id).id == "ckpt-11"
    assert Task.from_dict(retained[-1].snapshot).current_step == 11
    storage.close()


def test_normal_small_task_keeps_complete_checkpoint_history(tmp_path: Path) -> None:
    sandbox = tmp_path / "repo"
    sandbox.mkdir()
    (sandbox / "README.md").write_text("# small\n", encoding="utf-8")
    storage = SQLiteStorage(tmp_path / "small.db")
    registry = CapabilityRegistry()
    registry.register(FilesystemReadCapability(sandbox))
    planner = DeterministicPlanner()
    engine = ArionEngine(
        storage=storage,
        registry=registry,
        planner=planner,
        router=DeterministicRouter(planner),
        events=EventLogger(sinks=[storage]),
        policy=ResourcePolicy(
            boundaries={"filesystem:path": RelativePathBoundary()}
        ),
    )

    task = engine.execute_goal("summarize this repository")
    checkpoints = storage.list_checkpoints(task.id)

    assert len(checkpoints) == 4
    assert [checkpoint.reason for checkpoint in checkpoints] == [
        "plan produced", "step completed", "step completed", "task completed"
    ]
    assert checkpoints[-1].status == TaskStatus.COMPLETED.value
    engine.shutdown()
    storage.close()


def test_long_task_checkpoint_count_and_bytes_are_bounded(tmp_path: Path) -> None:
    count = 30
    capability = _PayloadCapability(payload_size=2048)
    planner = _ManyStepPlanner(count)
    engine, storage = _engine(tmp_path, capability, planner)

    task = engine.execute_goal("long bounded task")
    checkpoints = storage.list_checkpoints(task.id)
    task_json = json.dumps(storage.load_task(task.id).to_dict())
    checkpoint_bytes = sum(
        len(json.dumps(checkpoint.snapshot).encode("utf-8"))
        for checkpoint in checkpoints
    )

    assert task.status is TaskStatus.COMPLETED
    assert capability.calls == list(range(count))
    assert len(checkpoints) == DEFAULT_CHECKPOINT_RETENTION
    assert storage.latest_checkpoint(task.id).reason == "task completed"
    # Every retained checkpoint is a full snapshot no larger than the final
    # task, so count bounding also bounds total checkpoint bytes.
    assert checkpoint_bytes <= DEFAULT_CHECKPOINT_RETENTION * len(
        task_json.encode("utf-8")
    )

    task_id = task.id
    engine.shutdown()
    storage.close()

    resumed_capability = _PayloadCapability(payload_size=2048)
    resumed_engine, resumed_storage = _engine(
        tmp_path, resumed_capability, planner
    )
    resumed = resumed_engine.run_task(task_id)
    assert resumed.status is TaskStatus.COMPLETED
    assert resumed_capability.calls == []
    assert len(resumed_storage.list_checkpoints(task_id)) == DEFAULT_CHECKPOINT_RETENTION
    resumed_engine.shutdown()
    resumed_storage.close()


def test_pruning_failure_never_invalidates_new_checkpoint(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    capability = _PayloadCapability(payload_size=32)
    planner = _ManyStepPlanner(3)
    engine, storage = _engine(tmp_path, capability, planner)

    def fail_prune(*args, **kwargs):
        raise RuntimeError("retention unavailable")

    monkeypatch.setattr(storage, "prune_checkpoints", fail_prune)
    task = engine.execute_goal("pruning is best effort")
    latest = storage.latest_checkpoint(task.id)

    assert task.status is TaskStatus.COMPLETED
    assert latest is not None
    assert latest.reason == "task completed"
    assert Task.from_dict(latest.snapshot).status is TaskStatus.COMPLETED
    # In the safe failure mode history is larger, never missing.
    assert len(storage.list_checkpoints(task.id)) == 5
    engine.shutdown()
    storage.close()


class _SimulatedCrash(BaseException):
    pass


class _DurableMutationCapability:
    name = "checkpoint.mutate"
    description = "append an index exactly once before checkpoint"
    actions = [ActionSpec(
        name="append",
        description="append index",
        required_scope="checkpoint:write",
        side_effects="mutating",
        reversible=False,
        idempotent=False,
        retry_safe=False,
        param_schema={"index": {"type": "integer", "required": True}},
    )]

    def __init__(self, log_path: Path, crash_index: int | None = None):
        self.log_path = log_path
        self.crash_index = crash_index
        self.crashed = False
        self.calls: list[int] = []

    def execute(self, action, params):
        index = params["index"]
        self.calls.append(index)
        if index == self.crash_index and not self.crashed:
            self.crashed = True
            raise _SimulatedCrash()
        with self.log_path.open("a", encoding="utf-8") as handle:
            handle.write(f"{index}\n")
        return {"body": "applied", "index": index}


def test_retained_latest_snapshot_does_not_replay_completed_mutations(
    tmp_path: Path,
) -> None:
    count = 15
    crash_index = 12
    log_path = tmp_path / "mutations.log"
    planner = _ManyStepPlanner(
        count,
        capability="checkpoint.mutate",
        action="append",
        scope="checkpoint:write",
    )
    first_capability = _DurableMutationCapability(log_path, crash_index)
    first_engine, first_storage = _engine(
        tmp_path,
        first_capability,
        planner,
        db_name="mutation.db",
        scope="checkpoint:write",
    )
    goal = first_engine.submit_goal("durable mutations")
    task = first_engine.create_task(goal)

    with pytest.raises(_SimulatedCrash):
        first_engine.run_task(task.id)

    retained = first_storage.list_checkpoints(task.id)
    latest_before_restart = Task.from_dict(retained[-1].snapshot)
    assert len(retained) == DEFAULT_CHECKPOINT_RETENTION
    assert [step.status for step in latest_before_restart.steps[:crash_index]] == [
        StepStatus.SUCCEEDED
    ] * crash_index
    assert latest_before_restart.steps[crash_index].status is StepStatus.PENDING
    assert log_path.read_text(encoding="utf-8").splitlines() == [
        str(index) for index in range(crash_index)
    ]
    first_engine.shutdown()
    first_storage.close()

    resumed_capability = _DurableMutationCapability(log_path)
    resumed_engine, resumed_storage = _engine(
        tmp_path,
        resumed_capability,
        planner,
        db_name="mutation.db",
        scope="checkpoint:write",
    )
    resumed = resumed_engine.run_task(task.id)

    assert resumed.status is TaskStatus.COMPLETED
    assert resumed_capability.calls == list(range(crash_index, count))
    assert log_path.read_text(encoding="utf-8").splitlines() == [
        str(index) for index in range(count)
    ]
    assert len(resumed_storage.list_checkpoints(task.id)) == DEFAULT_CHECKPOINT_RETENTION
    assert resumed_engine.recovery_store.list_recoveries() == []
    resumed_engine.shutdown()
    resumed_storage.close()
