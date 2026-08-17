"""Explicit step_skipped semantics (architecture directive #2).

Skipped steps are NEVER silently deleted: they remain in the plan with
status SKIPPED, carry provenance, are audited (step.skipped), satisfy
dependencies, survive restart without re-execution, and the task always
reaches a terminal state (never a dangling 'planned' / RuntimeError).

Scenarios: all-skipped, partially-skipped, dependency interactions,
restart/resume, audit ordering, memory provenance.
"""

import pytest

from arion.capabilities.filesystem import FilesystemReadCapability
from arion.capabilities.registry import CapabilityRegistry
from arion.intelligence.planner import DeterministicPlanner
from arion.intelligence.router import DeterministicRouter
from arion.memory.guidance import MemoryGuidance
from arion.memory.reflector import DeterministicReflector
from arion.memory.store import SQLiteMemoryStore
from arion.observability.events import EventLogger
from arion.orchestration.authz import RelativePathBoundary, ResourcePolicy
from arion.orchestration.engine import ArionEngine
from arion.state.models import PlanStep, StepStatus, TaskStatus, VerificationPolicy
from arion.state.store import SQLiteStorage

FS = "filesystem:path"


def _engine(db_path, sandbox, memory=True):
    storage = SQLiteStorage(db_path)
    registry = CapabilityRegistry()
    registry.register(FilesystemReadCapability(sandbox))
    events = EventLogger(sinks=[storage])
    planner = DeterministicPlanner()
    return ArionEngine(
        storage=storage, registry=registry, planner=planner,
        router=DeterministicRouter(planner), events=events,
        policy=ResourcePolicy(boundaries={FS: RelativePathBoundary()}),
        memory=SQLiteMemoryStore(db_path) if memory else None,
        reflector=DeterministicReflector() if memory else None,
    ), storage


def _step(index, capability="filesystem.read", action="read", params=None, depends_on=None):
    return PlanStep(
        index=index, intent=f"s{index}", capability=capability, action=action,
        scope="filesystem:read", params=params or {"path": "README.md"},
        verification=VerificationPolicy("non_empty"),
        depends_on=list(depends_on or []),
    )


def _skip_step(step, reason="no safe alternative per memory guidance"):
    """Manually apply an explicit skip (same semantics as guidance)."""
    step.status = StepStatus.SKIPPED
    step.skipped_reason = reason
    step.guidance.append({
        "category": "step_skipped", "capability": step.capability, "action": step.action,
        "resource": step.params.get("path"), "guidance_id": "g_1",
        "episode_id": "ep_1", "reflection_id": "refl_1", "reason": reason,
    })
    return step


def _make_task(engine, steps):
    goal = engine.submit_goal("skip test")
    task = engine.create_task(goal)
    task.steps = steps
    engine.storage.save_task(task)
    return task


def test_all_steps_skipped_completes_with_audit(tmp_path, sandbox):
    engine, storage = _engine(tmp_path / "skip.db", sandbox)
    steps = [_skip_step(_step(0)), _skip_step(_step(1, action="list", params={"path": "."}))]
    task = _make_task(engine, steps)
    result = engine.run_task(task.id)

    # terminal state reached - never a dangling 'planned' or RuntimeError
    assert result.status == TaskStatus.COMPLETED
    assert all(s.status == StepStatus.SKIPPED for s in result.steps)
    # audit: step.skipped per skipped step + task.completed with skipped count
    kinds = [e.kind for e in storage.list_events(task.id)]
    assert kinds.count("step.skipped") == 2
    completed = [e for e in storage.list_events(task.id) if e.kind == "task.completed"][0]
    assert completed.detail["skipped_steps"] == 2
    # nothing was executed
    assert not [e for e in storage.list_events(task.id) if e.kind == "capability.executed"]
    storage.close()


def test_partially_skipped_executes_rest(tmp_path, sandbox):
    engine, storage = _engine(tmp_path / "skip.db", sandbox)
    steps = [
        _skip_step(_step(0, params={"path": "README.md"})),          # skipped
        _step(1, action="list", params={"path": "."}),               # executes
        _step(2, params={"path": "notes.txt"}),                      # executes
    ]
    task = _make_task(engine, steps)
    result = engine.run_task(task.id)

    assert result.status == TaskStatus.COMPLETED
    assert result.steps[0].status == StepStatus.SKIPPED
    assert result.steps[1].status == StepStatus.SUCCEEDED
    assert result.steps[2].status == StepStatus.SUCCEEDED
    # skipped step was NEVER executed; the others were
    executed = {e.step_id for e in storage.list_events(task.id) if e.kind == "capability.executed"}
    assert executed == {"step_1", "step_2"}
    assert "step_0" not in executed
    # audit ordering: skipped step audited before execution of the rest
    kinds = [e.kind for e in storage.list_events(task.id)]
    assert kinds.index("step.skipped") < kinds.index("capability.executed")
    storage.close()


def test_skipped_step_satisfies_dependencies(tmp_path, sandbox):
    """A skipped prerequisite is terminal: dependents still run."""
    engine, storage = _engine(tmp_path / "skip.db", sandbox)
    steps = [
        _skip_step(_step(0, params={"path": "README.md"})),           # skipped prereq
        _step(1, params={"path": "notes.txt"}, depends_on=[0]),       # dependent
    ]
    task = _make_task(engine, steps)
    result = engine.run_task(task.id)

    assert result.status == TaskStatus.COMPLETED
    assert result.steps[0].status == StepStatus.SKIPPED
    assert result.steps[1].status == StepStatus.SUCCEEDED  # ran despite skipped dep
    storage.close()


def test_restart_resume_skipped_not_reexecuted(db_path, sandbox, fresh_engine):
    """Checkpoint after a skip: a fresh process resumes and does NOT re-execute
    the skipped step (status persisted in the checkpoint)."""
    engine_a = fresh_engine(db_path, sandbox)
    steps = [
        _skip_step(_step(0, params={"path": "README.md"})),
        _step(1, action="list", params={"path": "."}),
    ]
    task = _make_task(engine_a, steps)
    task_id = task.id
    # run to completion in process A
    engine_a.run_task(task_id)
    # tamper: pretend we're mid-task - reset step 1 to pending, current_step 0,
    # so resume must re-walk from the start and see step 0 SKIPPED
    restored = engine_a.storage.load_task(task_id)
    restored.current_step = 0
    restored.status = TaskStatus.PLANNED
    restored.completed_at = None
    for s in restored.steps:
        if s.index != 0:
            s.status = StepStatus.PENDING
            s.result = None
    engine_a.storage.save_task(restored)
    engine_a.storage.close()

    engine_b = fresh_engine(db_path, sandbox)
    resumed = engine_b.run_task(task_id)
    assert resumed.status == TaskStatus.COMPLETED
    assert resumed.steps[0].status == StepStatus.SKIPPED  # preserved, not re-executed
    # THE INVARIANT: the skipped step is NEVER executed (in either process)
    kinds_b = [e.kind for e in engine_b.storage.list_events(task_id)]
    executed_b = {e.step_id for e in engine_b.storage.list_events(task_id) if e.kind == "capability.executed"}
    assert "step_0" not in executed_b
    assert "step_1" in executed_b
    assert kinds_b.count("task.resumed") == 1
    engine_b.storage.close()


def test_skipped_steps_persist_in_memory_provenance(tmp_path, sandbox):
    """Episode records the skipped step with its provenance (status + guidance)."""
    engine, storage = _engine(tmp_path / "skip.db", sandbox)
    steps = [_skip_step(_step(0, params={"path": "README.md"})), _step(1, action="list", params={"path": "."})]
    task = _make_task(engine, steps)
    engine.run_task(task.id)

    episodes = engine.memory.list_recent(limit=5)
    assert episodes
    ep = episodes[0]
    assert ep.outcome == "completed"
    step0 = next(s for s in ep.plan_summary if s["index"] == 0)
    assert step0["status"] == "skipped"
    # the episode's guidance provenance is recorded in the task's steps
    saved = engine.storage.load_task(task.id)
    assert saved.steps[0].guidance and saved.steps[0].guidance[0]["episode_id"] == "ep_1"
    storage.close()


def test_skipped_step_survives_plan_serialization(tmp_path, sandbox):
    engine, storage = _engine(tmp_path / "skip.db", sandbox)
    steps = [_skip_step(_step(0, params={"path": "README.md"})), _step(1, action="list", params={"path": "."})]
    task = _make_task(engine, steps)
    engine.run_task(task.id)
    # round-trip: task persisted as JSON with SKIPPED status + skipped_reason
    saved = engine.storage.load_task(task.id)
    assert saved.steps[0].status == StepStatus.SKIPPED
    assert saved.steps[0].skipped_reason is not None
    assert saved.steps[0].guidance
    storage.close()
