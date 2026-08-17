"""Dependency-aware execution tests (ADR-011, required fixes).

Seven required scenarios:
1. valid dependency chain
2. independent steps
3. dependency failure prevents dependent execution
4. invalid dependency rejected
5. dependency cycle rejected
6. restart/resume with dependencies
7. audit ordering follows dependency order
"""

import pytest

from arion.intelligence.errors import PlanValidationError
from arion.intelligence.plan_schema import PLAN_SCHEMA_VERSION, PlanSchema, StructuredStep
from arion.intelligence.plan_validator import PlanValidator, topo_sort_steps
from arion.state.models import PlanStep, StepStatus, TaskStatus, VerificationPolicy


def _step(idx, action="read", params=None, depends_on=None, verification=None, capability="filesystem.read"):
    return PlanStep(
        index=idx,
        intent=f"step{idx}",
        capability=capability,
        action=action,
        scope="filesystem:read",
        params=params if params is not None else {"path": "README.md"},
        verification=verification if verification is not None else VerificationPolicy("non_empty"),
        depends_on=list(depends_on or []),
    )


def _make_task(engine, steps):
    goal = engine.submit_goal("dependency test")
    task = engine.create_task(goal)
    task.steps = steps
    engine.storage.save_task(task)
    return task


def _started_order(engine, task_id):
    return [
        e.step_id
        for e in engine.storage.list_events(task_id)
        if e.kind == "step.started"
    ]


def _executed_steps(engine, task_id):
    return {e.step_id for e in engine.storage.list_events(task_id) if e.kind == "capability.executed"}


# ---------------------------------------------------------------------------
# 1. Valid dependency chain
# ---------------------------------------------------------------------------


def test_valid_dependency_chain(engine, sandbox, storage):
    steps = [
        _step(0, action="list", params={"path": "."}),
        _step(1, params={"path": "README.md"}, depends_on=[0]),
        _step(2, params={"path": "docs/design.md"}, depends_on=[1]),
    ]
    task = _make_task(engine, steps)
    result = engine.run_task(task.id)

    assert result.status == TaskStatus.COMPLETED
    assert all(s.status == StepStatus.SUCCEEDED for s in result.steps)
    # step 2's dependency chain was satisfied before it ran
    assert "content" in result.steps[2].result
    assert _started_order(engine, task.id) == ["step_0", "step_1", "step_2"]


# ---------------------------------------------------------------------------
# 2. Independent steps
# ---------------------------------------------------------------------------


def test_independent_steps_all_execute(engine, storage):
    steps = [
        _step(0, params={"path": "README.md"}),
        _step(1, params={"path": "notes.txt"}),
        _step(2, action="list", params={"path": "."}),
    ]
    task = _make_task(engine, steps)
    result = engine.run_task(task.id)

    assert result.status == TaskStatus.COMPLETED
    assert _started_order(engine, task.id) == ["step_0", "step_1", "step_2"]  # deterministic sequential
    assert all(s.status == StepStatus.SUCCEEDED for s in result.steps)


# ---------------------------------------------------------------------------
# 3. Dependency failure prevents dependent execution
# ---------------------------------------------------------------------------


def test_dependency_failure_prevents_dependents(engine, storage):
    steps = [
        _step(0, params={"path": "README.md"}),
        _step(1, params={"path": "does_not_exist.txt"}, depends_on=[0]),
        _step(2, params={"path": "notes.txt"}, depends_on=[1]),
    ]
    task = _make_task(engine, steps)
    result = engine.run_task(task.id)

    assert result.status == TaskStatus.FAILED
    assert result.error and "not a file" in result.error
    assert result.steps[1].status == StepStatus.FAILED
    # the DEPENDENT step never executed (step 1 itself ran and failed)
    assert result.steps[2].status == StepStatus.PENDING
    executed = _executed_steps(engine, task.id)
    assert "step_2" not in executed
    assert "step_1" in executed  # the failed step's attempt is still audited


# ---------------------------------------------------------------------------
# 4. Invalid dependencies
# ---------------------------------------------------------------------------


def test_invalid_forward_dependency_rejected_by_schema():
    """The plan schema forbids forward references (deps must be earlier steps)."""
    with pytest.raises(PlanValidationError, match="depends_on"):
        PlanSchema.from_dict({
            "version": PLAN_SCHEMA_VERSION,
            "intent": "x",
            "steps": [
                {"intent": "a", "capability": "c", "action": "a", "params": {},
                 "verification": {"policy": "non_empty"}, "depends_on": [1]},
                {"intent": "b", "capability": "c", "action": "a", "params": {},
                 "verification": {"policy": "non_empty"}},
            ],
        })


def test_invalid_out_of_range_dependency_rejected_by_validator(sandbox):
    registry = __import__("arion.capabilities.registry", fromlist=["CapabilityRegistry"]).CapabilityRegistry()
    registry.register(__import__("arion.capabilities.filesystem", fromlist=["FilesystemReadCapability"]).FilesystemReadCapability(sandbox))
    validator = PlanValidator(registry)
    schema = PlanSchema(
        version=PLAN_SCHEMA_VERSION,
        intent="x",
        steps=[StructuredStep(intent="a", capability="filesystem.read", action="read",
                              params={"path": "README.md"}, depends_on=[99])],
    )
    with pytest.raises(PlanValidationError, match="out-of-range"):
        validator.validate(schema)


def test_invalid_self_dependency_rejected(sandbox):
    registry = __import__("arion.capabilities.registry", fromlist=["CapabilityRegistry"]).CapabilityRegistry()
    registry.register(__import__("arion.capabilities.filesystem", fromlist=["FilesystemReadCapability"]).FilesystemReadCapability(sandbox))
    validator = PlanValidator(registry)
    schema = PlanSchema(
        version=PLAN_SCHEMA_VERSION,
        intent="x",
        steps=[StructuredStep(intent="a", capability="filesystem.read", action="read",
                              params={"path": "README.md"}, depends_on=[0])],
    )
    with pytest.raises(PlanValidationError, match="itself"):
        validator.validate(schema)


def test_invalid_dependency_fails_task_gracefully(engine, storage):
    """An impossible hand-built plan fails the task (defensive engine check)."""
    steps = [_step(0, params={"path": "README.md"}, depends_on=[2]), _step(1)]
    task = _make_task(engine, steps)
    result = engine.run_task(task.id)

    assert result.status == TaskStatus.FAILED
    assert "planning failed" in (result.error or "")
    assert "out-of-range" in (result.error or "")
    kinds = [e.kind for e in engine.storage.list_events(task.id)]
    assert "error" in kinds and "task.failed" in kinds


# ---------------------------------------------------------------------------
# 5. Dependency cycles
# ---------------------------------------------------------------------------


def test_dependency_cycle_rejected_by_validator(sandbox):
    registry = __import__("arion.capabilities.registry", fromlist=["CapabilityRegistry"]).CapabilityRegistry()
    registry.register(__import__("arion.capabilities.filesystem", fromlist=["FilesystemReadCapability"]).FilesystemReadCapability(sandbox))
    validator = PlanValidator(registry)
    schema = PlanSchema(
        version=PLAN_SCHEMA_VERSION,
        intent="cycle",
        steps=[
            StructuredStep(intent="a", capability="filesystem.read", action="read",
                           params={"path": "README.md"}, depends_on=[1]),
            StructuredStep(intent="b", capability="filesystem.read", action="read",
                           params={"path": "README.md"}, depends_on=[0]),
        ],
    )
    with pytest.raises(PlanValidationError, match="cycle"):
        validator.validate(schema)


def test_dependency_cycle_fails_task_gracefully(engine, storage):
    steps = [
        _step(0, params={"path": "README.md"}, depends_on=[1]),
        _step(1, params={"path": "notes.txt"}, depends_on=[0]),
    ]
    task = _make_task(engine, steps)
    result = engine.run_task(task.id)

    assert result.status == TaskStatus.FAILED
    assert "planning failed" in (result.error or "")
    assert "cycle" in (result.error or "")
    # nothing executed
    assert _executed_steps(engine, task.id) == set()


def test_topo_sort_returns_stable_order_for_dependency_free_plans():
    steps = [_step(0), _step(1), _step(2)]
    ordered = topo_sort_steps(steps)
    assert [s.index for s in ordered] == [0, 1, 2]


# ---------------------------------------------------------------------------
# 6. Restart/resume with dependencies
# ---------------------------------------------------------------------------


def test_restart_resume_with_dependencies(db_path, sandbox, fresh_engine):
    """Engine A persists a task with dependencies (planned, not executed);
    engine B resumes after 'restart' and completes in dependency order."""
    engine_a = fresh_engine(db_path, sandbox)
    steps = [
        _step(0, action="list", params={"path": "."}),
        _step(1, params={"path": "README.md"}, depends_on=[0]),
        _step(2, params={"path": "docs/design.md"}, depends_on=[1]),
    ]
    task = _make_task(engine_a, steps)
    task_id = task.id
    engine_a._checkpoint(task, reason="plan produced")  # "process dies" before executing
    engine_a.storage.close()

    engine_b = fresh_engine(db_path, sandbox)
    resumed = engine_b.run_task(task_id)

    assert resumed.status == TaskStatus.COMPLETED
    assert all(s.status == StepStatus.SUCCEEDED for s in resumed.steps)
    assert [e.kind for e in engine_b.storage.list_events(task_id)].count("task.resumed") == 1
    assert _started_order(engine_b, task_id) == ["step_0", "step_1", "step_2"]
    engine_b.storage.close()


# ---------------------------------------------------------------------------
# 7. Audit ordering
# ---------------------------------------------------------------------------


def test_audit_ordering_follows_dependencies(engine, storage):
    """step.started audit events must appear in dependency order."""
    steps = [
        _step(0, action="list", params={"path": "."}),
        _step(1, params={"path": "README.md"}, depends_on=[0]),
        _step(2, params={"path": "docs/design.md"}, depends_on=[1]),
    ]
    task = _make_task(engine, steps)
    engine.run_task(task.id)

    started = _started_order(engine, task.id)
    assert started == ["step_0", "step_1", "step_2"]
    # each step's audit event carries its dependency declaration
    events = {e.step_id: e for e in engine.storage.list_events(task.id) if e.kind == "step.started"}
    assert events["step_1"].detail["depends_on"] == [0]
    assert events["step_2"].detail["depends_on"] == [1]
